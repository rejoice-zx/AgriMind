"""VectorStore persistence (DS-05 / P1-VECTOR).

Covers the generation-based publish: an (index, map) pair is written as one
generation and committed by a single atomic manifest switch, so a crash or
failure never leaves index and map out of sync, the previous generation stays
readable for rollback, and the legacy (pre-generation) layout stays loadable.
"""
import json
import os

import pytest

from app.core.vector_store import VectorStore
from app.core import vector_store as vector_module

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


def test_save_publishes_generation_pair_and_manifest(isolated_store):
    store = isolated_store
    store.add_vector("a", _unit(0))
    store.add_vector("b", _unit(1))
    assert store.save() is True

    with open(store._manifest_file, encoding="utf-8") as f:
        manifest = json.load(f)
    generation = manifest["generation"]
    assert manifest["index_file"] == f"vectors_gen_{generation}.index"
    assert manifest["map_file"] == f"vectors_gen_{generation}.map.json"
    # both halves of the same generation are on disk (the pair is published together)
    assert os.path.exists(os.path.join(store._data_dir, manifest["index_file"]))
    assert os.path.exists(os.path.join(store._data_dir, manifest["map_file"]))
    # the store now points at the published generation, and no temp file leaks
    assert store._map_file == os.path.join(store._data_dir, manifest["map_file"])
    assert not os.path.exists(store._manifest_file + ".tmp")


def test_reload_after_publish_restores_items(isolated_store):
    store = isolated_store
    store.add_vector("a", _unit(0))
    store.add_vector("b", _unit(1))
    assert store.save() is True

    store.clear()
    assert store.count == 0
    store._load_index()
    assert store.count == 2
    assert store.has_item("a") and store.has_item("b")


def test_save_returns_false_without_pretending_success(isolated_store, monkeypatch):
    store = isolated_store
    store.add_vector("a", _unit(0))

    def boom(*args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", boom)
    assert store.save() is False


def test_failed_manifest_switch_preserves_previous_generation(isolated_store, monkeypatch):
    store = isolated_store
    store.add_vector("a", _unit(0))
    assert store.save() is True
    published_index, published_map = store._index_file, store._map_file

    # Second save: the new generation files land, but the manifest switch fails.
    store.add_vector("b", _unit(1))
    real_replace = os.replace

    def flaky(src, dst):
        if str(dst).endswith("manifest.json"):
            raise OSError("disk full (simulated)")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    assert store.save() is False
    monkeypatch.undo()

    # The previously published generation is untouched...
    assert os.path.exists(published_index) and os.path.exists(published_map)
    # ...no half-published generation is left behind...
    leftovers = [n for n in os.listdir(store._data_dir) if n.endswith(".tmp")]
    assert leftovers == []
    # ...and a reload still yields the complete old data set.
    store.clear()
    store._load_index()
    assert store.count == 1
    assert store.has_item("a") and not store.has_item("b")


def test_corrupt_manifest_falls_back_to_legacy(isolated_store):
    store = isolated_store
    os.makedirs(store._data_dir, exist_ok=True)
    store.add_vector("legacy", _unit(0))
    vector_module.faiss.write_index(store._index, store._legacy_index_file)
    with open(store._legacy_map_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "id_map": {"0": "legacy"},
                "reverse_map": {"legacy": 0},
                "next_id": 1,
                "dimension": DIM,
                "index_type": "flat",
                "is_trained": True,
            },
            f,
        )
    with open(store._manifest_file, "w", encoding="utf-8") as f:
        f.write("{ this is not valid json")

    store.clear()
    store._load_index()  # must not raise
    assert store.has_item("legacy")


def test_corrupt_map_file_does_not_crash_on_load(isolated_store):
    store = isolated_store
    os.makedirs(store._data_dir, exist_ok=True)
    with open(store._legacy_map_file, "w", encoding="utf-8") as f:
        f.write("{ this is not valid json")

    store.clear()
    store._load_index()  # must not raise
    assert store.count == 0


def test_old_generations_are_garbage_collected(isolated_store):
    store = isolated_store
    for i in range(5):
        store.add_vector(f"v{i}", _unit(i))
        assert store.save() is True

    maps = [n for n in os.listdir(store._data_dir) if n.startswith("vectors_gen_") and n.endswith(".map.json")]
    assert len(maps) <= store._generations_to_keep

    with open(store._manifest_file, encoding="utf-8") as f:
        current_map = json.load(f)["map_file"]
    assert os.path.exists(os.path.join(store._data_dir, current_map))

    store.clear()
    store._load_index()
    assert store.count == 5
