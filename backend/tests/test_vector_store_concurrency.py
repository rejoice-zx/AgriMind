"""VectorStore thread-safety (DS-05 / P1-VECTOR).

VectorStore is a process-wide singleton driven by a ThreadPoolExecutor in
vector_service, so its mutable index/id-map state must be mutually exclusive,
and a rebuild must publish the whole new index in one atomic swap: a concurrent
reader may see the complete old index or the complete new index, never a
partially rebuilt one.
"""
import threading

import pytest

from app.core.vector_store import VectorStore

DIM = 8


def _unit(i: int):
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    return v


@pytest.fixture
def isolated_store(tmp_path):
    store = VectorStore()
    orig = (store._data_dir, store._index_file, store._map_file, store._dimension)
    store._data_dir = str(tmp_path)
    store._dimension = DIM
    store.clear()
    try:
        yield store
    finally:
        store._data_dir = orig[0]
        store._index_file = orig[1]
        store._map_file = orig[2]
        store._dimension = orig[3]
        store.clear()


def test_concurrent_add_search_remove_rebuild_stay_consistent(isolated_store):
    store = isolated_store
    errors = []
    barrier = threading.Barrier(8)

    def worker(i):
        try:
            barrier.wait()
            for j in range(40):
                v = _unit(i + j)
                store.add_vector(f"t{i}_{j}", v)
                store.search(v, top_k=3)
                if j % 7 == 0:
                    store.remove_vector(f"t{i}_{j}")
                if j % 13 == 0:
                    store.rebuild_index(vectors_data=store._snapshot_vectors())
        except Exception as exc:  # noqa: BLE001 - record, don't swallow the assertion
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # id_map / reverse_map must stay consistent with each other.
    assert len(store._id_map) == len(store._reverse_map)
    assert store.count == len(store._reverse_map)


def test_rebuild_publishes_atomically(isolated_store, monkeypatch):
    """Sample while a candidate is being built, then after its publication."""
    store = isolated_store
    for i in range(30):
        store.add_vector(f"old{i}", _unit(i))

    observed = []
    stop = threading.Event()
    building = threading.Event()
    sampled_old = threading.Event()
    sampled_new = threading.Event()
    original_build = store._build_index_objects

    def coordinated_build(vectors_data, index_type, dimension):
        building.set()
        assert sampled_old.wait(5), "reader could not sample the live old index during candidate construction"
        return original_build(vectors_data, index_type, dimension)

    monkeypatch.setattr(store, "_build_index_objects", coordinated_build)

    def reader():
        if not building.wait(5):
            return
        while not stop.is_set():
            count = store.count
            observed.append(count)
            if count == 30:
                sampled_old.set()
            if count == 40:
                sampled_new.set()
            stop.wait(0.001)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        new_data = [(f"new{i}", _unit(i)) for i in range(40)]
        assert store.rebuild_index(vectors_data=new_data) is True
        assert sampled_new.wait(5), "reader could not sample the published new index"
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert store.count == 40
    # Both generations were sampled, and no partial generation was visible.
    assert set(observed) == {30, 40}, sorted(set(observed))


def test_rebuild_aborts_when_a_write_races_it(isolated_store):
    """An off-lock rebuild must not clobber a write that landed during the build."""
    store = isolated_store
    for i in range(20):
        store.add_vector(f"seed{i}", _unit(i))

    original_build = store._build_index_objects
    injected = {"done": False}

    def build_with_concurrent_write(vectors_data, index_type, dimension):
        if not injected["done"]:
            injected["done"] = True
            store.add_vector("late", _unit(3))  # lands while the rebuild builds
        return original_build(vectors_data, index_type, dimension)

    store._build_index_objects = build_with_concurrent_write
    try:
        assert store.rebuild_index(vectors_data=store._snapshot_vectors()) is False
    finally:
        store._build_index_objects = original_build

    # The racing write was preserved rather than dropped by the aborted rebuild.
    assert store.has_item("late")
