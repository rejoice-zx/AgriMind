"""Rebuild/publish/recovery regressions from independent native-FAISS probes.

Every store is independent and every file lives in tmp_path. Embedding and DB
fixtures are isolated; no provider HTTP or production settings/data are consulted.
"""
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from app.core import vector_store as module
from app.services import vector_service as service_module
from app.services.knowledge_service import KnowledgeService
from app.services.vector_service import VectorService
from app.models.knowledge import KnowledgeItem

pytestmark = pytest.mark.skipif(not module.FAISS_AVAILABLE, reason="native FAISS regression")


@pytest.fixture
def store(tmp_path):
    value = object.__new__(module.VectorStore)
    value._initialized = True
    value._lock = threading.RLock()
    value._mutation_seq = 0
    value._dimension = 2
    value._index = None
    value._index_type = module.IndexType.FLAT
    value._id_map, value._reverse_map = {}, {}
    value._next_id = 0
    value._pending_vectors = []
    value._built_embedding_model = "old-model"
    value._last_error = None
    value._is_trained = True
    value._generations_to_keep = 2
    value._data_dir = str(tmp_path)
    value._get_current_embedding_model = lambda: "old-model"
    value._create_index(2)
    assert value.add_vector("old", [1.0, 0.0])
    assert value.save()
    return value


@pytest.fixture
def knowledge(store, monkeypatch):
    config = {"model": "new-model"}
    client = SimpleNamespace(
        _get_model_name=lambda: config["model"],
        _get_api_url=lambda: "https://mock.invalid/v1",
        _get_api_key=lambda: "synthetic-only",
    )
    monkeypatch.setattr(service_module, "_vector_store", store)
    monkeypatch.setattr(service_module, "_embedding_client", client)
    value = KnowledgeService()

    async def sync(_db):
        return True

    monkeypatch.setattr(value.vector_service, "sync_config_from_db", sync)
    monkeypatch.setattr(value, "_build_chunks",
                        lambda item: [(item.id + "#chunk_0", "mock"), (item.id + "#chunk_1", "mock")])
    return value, config


async def _item(db):
    db.add(KnowledgeItem(id="item", question="mock", answer="mock"))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, [], [[1.0, 0.0]], [[1.0, 0.0], None]])
async def test_incomplete_embeddings_keep_memory_and_disk(store, knowledge, test_session,
                                                          monkeypatch, response):
    value, _ = knowledge
    await _item(test_session)
    before = open(store._manifest_file, "rb").read()

    async def embed(_texts):
        return response

    monkeypatch.setattr(value.vector_service, "embed_texts", embed)
    count, errors = await value.rebuild_index(test_session)
    assert count == 0 and errors
    assert store.has_item("old") and store.search([1.0, 0.0])[0][0] == "old"
    assert open(store._manifest_file, "rb").read() == before


@pytest.mark.asyncio
async def test_rebuild_changes_dimension_and_records_actual_model(store, knowledge,
                                                                  test_session, monkeypatch):
    value, _ = knowledge
    await _item(test_session)

    async def embed(_texts):
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]

    monkeypatch.setattr(value.vector_service, "embed_texts", embed)
    assert await value.rebuild_index(test_session) == (1, [])
    assert store.dimension == 3 and store.embedding_model == "new-model"
    assert store.has_item("item#chunk_0") and not store.has_item("old")
    store.clear()
    store._load_index()
    assert store.dimension == 3 and store.embedding_model == "new-model"
    assert store.has_item("item#chunk_0")


@pytest.mark.asyncio
async def test_write_during_collection_rejects_candidate(store, knowledge, test_session, monkeypatch):
    value, _ = knowledge
    await _item(test_session)
    before = open(store._manifest_file, "rb").read()

    async def embed(_texts):
        store.add_vector("late", [0.0, 1.0])
        return [[1.0, 0.0], [0.0, 1.0]]

    monkeypatch.setattr(value.vector_service, "embed_texts", embed)
    count, errors = await value.rebuild_index(test_session)
    assert count == 0 and errors
    assert store.has_item("old") and store.has_item("late")
    assert open(store._manifest_file, "rb").read() == before


@pytest.mark.asyncio
async def test_model_change_during_collection_rejects_candidate(store, knowledge, test_session, monkeypatch):
    value, config = knowledge
    await _item(test_session)

    async def embed(_texts):
        config["model"] = "another-model"
        return [[1.0, 0.0], [0.0, 1.0]]

    monkeypatch.setattr(value.vector_service, "embed_texts", embed)
    count, errors = await value.rebuild_index(test_session)
    assert count == 0 and errors and store.has_item("old")
    assert store.embedding_model == "old-model"


@pytest.mark.asyncio
async def test_model_changes_back_during_collection_still_reject_candidate(
        store, knowledge, test_session, monkeypatch):
    from app.core.embedding import EmbeddingClient
    value, _ = knowledge
    client = object.__new__(EmbeddingClient)
    client._initialized = False
    EmbeddingClient.__init__(client)
    client.configure(api_key="synthetic", api_url="https://mock.invalid/v1", model="model-a", dimension=2)
    monkeypatch.setattr(service_module, "_embedding_client", client)
    await _item(test_session)

    async def embed(_texts):
        client.configure(model="model-b")
        # Even if the final model is A again, some collected vectors may be B.
        client.configure(model="model-a")
        return [[1.0, 0.0], [0.0, 1.0]]

    monkeypatch.setattr(value.vector_service, "embed_texts", embed)
    count, errors = await value.rebuild_index(test_session)
    assert count == 0 and errors and store.has_item("old")
    assert store.embedding_model == "old-model"


@pytest.mark.asyncio
async def test_publication_failure_is_not_reported_as_success(store, knowledge, test_session, monkeypatch):
    value, _ = knowledge
    await _item(test_session)
    before = open(store._manifest_file, "rb").read()

    async def embed(_texts):
        return [[1.0, 0.0], [0.0, 1.0]]

    monkeypatch.setattr(value.vector_service, "embed_texts", embed)
    monkeypatch.setattr(store, "_write_manifest", lambda *args: False)
    count, errors = await value.rebuild_index(test_session)
    assert count == 0 and errors and store.has_item("old")
    assert not store.has_item("item#chunk_0")
    assert open(store._manifest_file, "rb").read() == before
    store.clear()
    store._load_index()
    assert store.has_item("old")


@pytest.mark.asyncio
async def test_vector_service_propagates_save_failure(store, knowledge, monkeypatch):
    value, _ = knowledge
    monkeypatch.setattr(store, "save", lambda: False)
    assert await value.vector_service.save() is False


@pytest.mark.asyncio
async def test_empty_database_commits_complete_empty_generation(store, knowledge, test_session):
    value, _ = knowledge
    assert await value.rebuild_index(test_session) == (0, [])
    assert store.count == 0 and not store.has_item("old")
    store._load_index()
    assert store.count == 0 and store._index.ntotal == 0


@pytest.mark.parametrize("failure", ["index_write", "index_fsync", "map_replace", "manifest_replace"])
def test_failed_candidate_never_changes_live_or_published_state(store, monkeypatch, failure):
    before = open(store._manifest_file, "rb").read()
    if failure == "index_write":
        def fail(*args):
            raise OSError("synthetic write failure")
        monkeypatch.setattr(module.faiss, "write_index", fail)
    elif failure == "index_fsync":
        # The first fsync in the candidate transaction is the native index file.
        def fail(*args):
            raise OSError("synthetic flush failure")
        monkeypatch.setattr(module.os, "fsync", fail)
    else:
        replace = module.os.replace
        def fail(src, dst):
            if ((failure == "map_replace" and str(dst).endswith(".map.json"))
                    or (failure == "manifest_replace" and str(dst).endswith("manifest.json"))):
                raise OSError("synthetic atomic switch failure")
            return replace(src, dst)
        monkeypatch.setattr(module.os, "replace", fail)
    assert store.rebuild_index(vectors_data=[("new", [0.0, 1.0])],
                               expected_revision=store.revision(), persist=True) is False
    assert store.has_item("old") and not store.has_item("new")
    assert open(store._manifest_file, "rb").read() == before


@pytest.mark.parametrize("corrupt", ["index", "map", "manifest"])
def test_corrupt_current_generation_recovers_published_predecessor(store, corrupt):
    previous_index = store._index_file
    store.add_vector("new", [0.0, 1.0])
    assert store.save()
    path = {"index": store._index_file, "map": store._map_file, "manifest": store._manifest_file}[corrupt]
    with open(path, "wb") as stream:
        stream.write(b"synthetic corruption")
    store.clear()
    store._load_index()
    assert store.count == 1 and store.has_item("old") and not store.has_item("new")
    assert store._index_file == previous_index
    assert store.search([1.0, 0.0])[0][0] == "old"


@pytest.mark.parametrize("invalid", ["empty", "reverse_string", "dimension", "native_ids", "next_bool"])
def test_metadata_and_native_ids_are_validated_even_with_matching_checksum(store, invalid):
    with open(store._map_file, encoding="utf-8") as stream:
        data = json.load(stream)
    if invalid == "empty":
        data = {}
    elif invalid == "reverse_string":
        data["reverse_map"]["old"] = "0"
    elif invalid == "dimension":
        data["dimension"] = 3
    elif invalid == "native_ids":
        data["id_map"], data["reverse_map"], data["next_id"] = {"7": "old"}, {"old": 7}, 8
    else:
        data["next_id"] = True
    with open(store._map_file, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    with open(store._manifest_file, encoding="utf-8") as stream:
        manifest = json.load(stream)
    manifest["map_sha256"] = hashlib.sha256(open(store._map_file, "rb").read()).hexdigest()
    with open(store._manifest_file, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream)
    store.clear()
    store._load_index()
    assert store.count == 0 and store.needs_rebuild()[0] is True
    assert store.last_error["type"] == "invalid_generation"


@pytest.mark.parametrize("kind,count", [(module.IndexType.FLAT, 3),
                                       (module.IndexType.IVF, 50),
                                       (module.IndexType.HNSW, 3)])
def test_native_type_round_trip_and_id_map_validation(store, kind, count):
    data = [(f"item{i}", [1.0, float(i + 1)]) for i in range(count)]
    assert store.rebuild_index(kind, data, persist=True)
    store.clear()
    store._load_index()
    assert store.count == count and store.index_type == kind
    assert store.has_item("item0") and store.dimension == 2


def test_duplicate_and_bad_vectors_abort_without_publication(store):
    for data in [
        [("a", [1.0, 0.0]), ("a", [0.0, 1.0])],
        [("a", [float("nan"), 0.0])],
        [("a", [1.0, 0.0]), ("b", [1.0, 0.0, 0.0])],
    ]:
        assert store.rebuild_index(vectors_data=data, persist=True) is False
        assert store.has_item("old")


def test_corrupt_first_manifest_is_explicitly_unavailable(store):
    with open(store._manifest_file, "wb") as stream:
        stream.write(b"synthetic corrupt first manifest")
    store.clear()
    store._load_index()
    assert store.count == 0 and store.needs_rebuild()[0] is True
    assert store.last_error["type"] == "invalid_generation"


def test_numpy_snapshot_compacts_deleted_positions_and_recovers(store, monkeypatch):
    monkeypatch.setattr(module, "FAISS_AVAILABLE", False)
    store.clear()
    assert store.add_vector("deleted", [1.0, 0.0])
    assert store.add_vector("kept", [0.0, 1.0])
    assert store.remove_vector("deleted")
    assert store.search([0.0, 1.0])[0][0] == "kept"
    assert store.save() is True
    store.clear()
    store._load_index()
    assert store.count == 1 and store.search([0.0, 1.0])[0][0] == "kept"
    assert store.needs_rebuild()[0] is False


def test_numpy_rejects_dimension_change_on_nonempty_incremental_index(store, monkeypatch):
    monkeypatch.setattr(module, "FAISS_AVAILABLE", False)
    store.clear()
    store.add_vector("old", [1.0, 0.0])
    assert store.add_vector("bad", [1.0, 0.0, 0.0]) is False
    assert store.dimension == 2 and store.search([1.0, 0.0])[0][0] == "old"


def test_failed_ordinary_save_marks_unsaved_memory_and_retains_disk(store, monkeypatch):
    store.add_vector("unsaved", [0.0, 1.0])
    before = open(store._manifest_file, "rb").read()
    real_commit = store._write_manifest
    monkeypatch.setattr(store, "_write_manifest", lambda *args: False)
    assert store.save() is False
    assert store.has_item("unsaved")
    assert store.last_error["type"] == "persistence_failure"
    assert open(store._manifest_file, "rb").read() == before
    monkeypatch.setattr(store, "_write_manifest", real_commit)
    assert store.save() is True
    assert store.last_error is None
    store._load_index()
    assert store.has_item("unsaved")


def test_native_index_is_fsynced_before_manifest_commit(store, monkeypatch):
    seen = []
    raw_open = open
    fd_names = {}
    class Tracked:
        def __init__(self, path, *args, **kwargs):
            self.path = str(path)
            self.stream = raw_open(path, *args, **kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def fileno(self):
            fd = self.stream.fileno()
            fd_names[fd] = self.path
            return fd
    fsync = module.os.fsync
    def record(fd):
        seen.append(fd_names.get(fd, "directory"))
        return fsync(fd)
    monkeypatch.setattr(module, "open", Tracked, raising=False)
    monkeypatch.setattr(module.os, "fsync", record)
    assert store.rebuild_index(vectors_data=[("new", [1.0, 0.0])], persist=True)
    assert seen[0].endswith(".index.tmp")
    assert next(i for i, path in enumerate(seen) if path.endswith(".map.json.tmp")) < next(
        i for i, path in enumerate(seen) if path.endswith("manifest.json.tmp"))


@pytest.mark.asyncio
async def test_sse_reports_abort_not_done_on_incomplete_embedding(store, knowledge, test_session,
                                                                monkeypatch):
    from app.api import knowledge as api
    from app.services.settings_service import SettingsService, RAGConfig
    value, _ = knowledge
    await _item(test_session)
    monkeypatch.setattr(api, "knowledge_service", value)
    async def rag_config(_db):
        return RAGConfig()
    async def embed(_texts):
        return None
    monkeypatch.setattr(SettingsService, "get_rag_config", rag_config)
    monkeypatch.setattr(value.vector_service, "embed_texts", embed)
    response = await api.rebuild_index(db=test_session, current_user=None)
    events = [part async for part in response.body_iterator]
    assert any('"type": "error"' in part for part in events)
    assert not any('"type": "done"' in part for part in events)
    assert store.has_item("old")

