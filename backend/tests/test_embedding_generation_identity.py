"""Independent DB/storage, deterministic vectors and blocked real HTTP verify space identity."""
import asyncio
import json
import threading
from types import SimpleNamespace
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.database import Base, create_database_engine
from app.models import KnowledgeItem, User
from app.services.settings_service import SettingsService as Settings, EmbeddingConfig, AgriAIConfig, LLMConfig, RAGConfig
from app.services.vector_service import VectorService
from app.services.knowledge_service import KnowledgeService
from app.services.agri_retrieval_service import AgriRetrievalService
from app.core.embedding import EmbeddingClient
from app.core.config import Config
from app.services import vector_service as vector_module
from app.config import settings
from tests.test_vector_generation_transactions import store


@pytest_asyncio.fixture
async def identity(tmp_path, monkeypatch, store, controlled_dns):
    engine = create_database_engine('sqlite+aiosqlite:///' + (tmp_path / 'identity.db').as_posix())
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = object.__new__(EmbeddingClient); client._initialized = False; EmbeddingClient.__init__(client)
    monkeypatch.setattr(vector_module, '_embedding_client', client)
    monkeypatch.setattr(vector_module, '_vector_store', store)
    controlled_dns({'embed.example': ['93.184.216.34'], 'other.example': ['93.184.216.35']})
    monkeypatch.setattr(settings, 'EMBEDDING_API_KEY', '')
    monkeypatch.setattr(settings, 'LLM_API_KEY', '')
    calls = []
    def post(url, headers, json, **kwargs):
        calls.append((url, headers['Authorization'], dict(json)))
        rows = [{'index': index, 'embedding': [1.0, 0.0]} for index, _ in enumerate(json['input'])]
        return SimpleNamespace(status_code=200, json=lambda: {'data': list(reversed(rows))})
    monkeypatch.setattr('app.core.embedding.requests.post', post)
    async with sessions() as db:
        await Settings.set_embedding_config(db, EmbeddingConfig(provider='siliconflow', api_url='https://embed.example/v1',
            api_key='synthetic-key-A', model='model-A', dimension=2))
        await Settings.set_rag_config(db, RAGConfig(chunk_size=500, chunk_overlap=50, similarity_threshold=0))
        db.add(KnowledgeItem(id='identity-kb', question='番茄病叶需复核', answer='补充整株照片，资料不足时人工复核', crop='番茄', is_current=1, source_type='test_fixture'))
        await db.commit()
        yield SimpleNamespace(db=db, sessions=sessions, store=store, client=client, calls=calls)
    await engine.dispose()


@pytest.mark.asyncio
async def test_persisted_default_name_is_not_overridden_and_foreign_keys_are_not_borrowed(identity, monkeypatch):
    env = identity
    monkeypatch.setattr(settings, 'EMBEDDING_MODEL', 'environment-model')
    await Settings.set_embedding_config(env.db, EmbeddingConfig(api_url='https://embed.example/v1',
        api_key='', model='BAAI/bge-large-zh-v1.5', dimension=2))
    await Settings.set_llm_config(env.db, LLMConfig(api_key='synthetic-legacy-key', api_url='https://other.example/v1'))
    await Settings.set_agri_ai_config(env.db, AgriAIConfig(llm_api_key='synthetic-agri-key', llm_base_url='https://other.example/v1'))
    resolved = await Settings.get_embedding_config_with_env(env.db)
    assert resolved.model == 'BAAI/bge-large-zh-v1.5' and resolved.api_key == ''
    await Settings.set_agri_ai_config(env.db, AgriAIConfig(llm_api_key='synthetic-agri-key', llm_base_url='https://embed.example/v1'))
    assert (await Settings.get_embedding_config_with_env(env.db)).api_key == 'synthetic-agri-key'


@pytest.mark.asyncio
async def test_inflight_client_keeps_url_model_key_and_cache_of_its_own_snapshot(identity, monkeypatch):
    env = identity
    a = VectorService(); assert await a.sync_config_from_db(env.db)
    entered, release = threading.Event(), threading.Event()
    def post(url, headers, json, **kwargs):
        entered.set(); release.wait(3)
        env.calls.append((url, headers['Authorization'], dict(json)))
        return SimpleNamespace(status_code=200, json=lambda: {'data': [{'index': 0, 'embedding': [1.0, 0.0]}]})
    monkeypatch.setattr('app.core.embedding.requests.post', post)
    pending = asyncio.create_task(a.embed_text('same question'))
    assert await asyncio.to_thread(entered.wait, 2)
    await Settings.set_embedding_config(env.db, EmbeddingConfig(api_url='https://other.example/v1', api_key='synthetic-key-B', model='model-B', dimension=2))
    b = VectorService(); assert await b.sync_config_from_db(env.db)
    release.set(); assert await pending == [1.0, 0.0]
    assert env.calls[0][0] == 'https://embed.example/v1/embeddings'
    assert env.calls[0][1] == 'Bearer synthetic-key-A' and env.calls[0][2]['model'] == 'model-A'
    assert await a.embed_text('same question') == [1.0, 0.0] and len(env.calls) == 1
    assert await b.embed_text('same question') == [1.0, 0.0] and len(env.calls) == 2
    assert env.calls[1][1] == 'Bearer synthetic-key-B'


@pytest.mark.asyncio
async def test_identity_survives_reload_and_same_dimension_model_change_disables_dense(identity):
    env = identity
    worker = KnowledgeService()
    assert await worker.rebuild_index(env.db) == (1, [])
    expected = env.store.get_index_info()['embedding_contract']
    assert expected['model'] == 'model-A' and expected['dimension'] == 2
    env.store.clear(); env.store._load_index()
    assert env.store.get_index_info()['embedding_contract'] == expected
    a = VectorService(); assert await a.sync_config_from_db(env.db); assert a.compatibility()[0]
    await Settings.set_embedding_config(env.db, EmbeddingConfig(api_url='https://embed.example/v1', api_key='synthetic-key-A', model='model-B', dimension=2))
    result, trace = await AgriRetrievalService.search(env.db, '番茄病叶')
    assert result.retrieval_path == 'bm25' and result.retrieved_items
    assert trace['vector_index_compatible'] is False and '不一致' in trace['vector_index_reason']
    assert env.store.embedding_model == 'model-A'


@pytest.mark.asyncio
async def test_chunk_or_knowledge_changes_disable_dense_without_mutating_old_index(identity):
    env = identity
    assert await KnowledgeService().rebuild_index(env.db) == (1, [])
    before = open(env.store._manifest_file, 'rb').read()
    await Settings.set_rag_config(env.db, RAGConfig(chunk_size=1000, chunk_overlap=50))
    vector = VectorService(); assert await vector.sync_config_from_db(env.db)
    assert not vector.compatibility()[0]
    await Settings.set_rag_config(env.db, RAGConfig(chunk_size=500, chunk_overlap=50))
    await env.db.execute(update(KnowledgeItem).values(answer='修正后的真实正文')); await env.db.commit()
    vector = VectorService(); assert await vector.sync_config_from_db(env.db)
    assert not vector.compatibility()[0] and '正文' in vector.compatibility()[1]
    assert open(env.store._manifest_file, 'rb').read() == before


@pytest.mark.asyncio
async def test_rebuild_uses_frozen_chunks_and_rejects_a_b_a_settings_change(identity, monkeypatch):
    env = identity
    worker = KnowledgeService()
    before = open(env.store._manifest_file, 'rb').read()
    seen = []
    async def embed(texts):
        seen.append(worker._get_chunk_config())
        await Settings.set_rag_config(env.db, RAGConfig(chunk_size=1000, chunk_overlap=10))
        Config().set('chunk_size', 1000)
        await Settings.set_rag_config(env.db, RAGConfig(chunk_size=500, chunk_overlap=50))
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(worker.vector_service, 'embed_texts', embed)
    count, errors = await worker.rebuild_index(env.db)
    assert count == 0 and errors and seen == [(500, 50, 10)]
    assert open(env.store._manifest_file, 'rb').read() == before


@pytest.mark.asyncio
async def test_incomplete_or_wrong_dimension_provider_response_cannot_publish(identity, monkeypatch):
    env = identity
    before = open(env.store._manifest_file, 'rb').read()
    def post(*args, **kwargs):
        return SimpleNamespace(status_code=200, json=lambda: {'data': [{'index': 0, 'embedding': [1.0, 0.0, 0.0]}]})
    monkeypatch.setattr('app.core.embedding.requests.post', post)
    count, errors = await KnowledgeService().rebuild_index(env.db)
    assert count == 0 and errors and open(env.store._manifest_file, 'rb').read() == before
    assert env.client.dimension == 2


@pytest.mark.asyncio
async def test_known_legacy_generation_is_not_labeled_current_or_sent_to_embedding(identity):
    env = identity
    result, trace = await AgriRetrievalService.search(env.db, '番茄病叶')
    assert result.retrieved_items and result.retrieval_path == 'bm25' and not env.calls
    assert not trace['vector_index_compatible'] and '旧索引' in trace['vector_index_reason']


@pytest.mark.asyncio
async def test_dimensions_are_optional_request_policy_and_responses_follow_index_order(identity, monkeypatch):
    env = identity
    await Settings.set_embedding_config(env.db, EmbeddingConfig(api_url='https://embed.example/v1', api_key='synthetic-key-A',
        model='model-A', dimension=2, request_dimensions=True))
    def post(url, headers, json, **kwargs):
        assert json['dimensions'] == 2
        return SimpleNamespace(status_code=200, json=lambda: {'data': [{'index': 1, 'embedding': [0.0, 1.0]}, {'index': 0, 'embedding': [1.0, 0.0]}]})
    monkeypatch.setattr('app.core.embedding.requests.post', post)
    vector = VectorService(); assert await vector.sync_config_from_db(env.db)
    assert await vector.embed_texts(['first', 'second']) == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.asyncio
async def test_complete_generation_requires_no_credentials_in_metadata_and_sql_edits_preserve_old_file(identity):
    env = identity
    assert await KnowledgeService().rebuild_index(env.db) == (1, [])
    info = env.store.get_index_info()
    assert 'synthetic-key' not in json.dumps(info)
    before = open(env.store._manifest_file, 'rb').read()
    item = await env.db.get(KnowledgeItem, 'identity-kb')
    item.answer = '新正文'; await env.db.commit()
    assert await KnowledgeService()._sync_vector_update(item, db=env.db) is False
    assert open(env.store._manifest_file, 'rb').read() == before


@pytest.mark.asyncio
async def test_invalid_saved_embedding_or_url_is_unavailable_without_fallback(identity):
    env = identity
    for value in ('not-json', json.dumps({'model': 'saved', 'unknown': True}), json.dumps({'api_url': 'https://name:password@embed.example/v1'})):
        await Settings.set_setting(env.db, Settings.KEY_EMBEDDING_CONFIG, value)
        vector = VectorService()
        assert not await vector.sync_config_from_db(env.db) and await vector.embed_text('question') is None
    assert not env.calls


@pytest.mark.asyncio
async def test_index_http_status_reports_built_model_instead_of_newly_selected_model(identity):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from app.api.knowledge import router
    from app.api.dependencies import get_current_user
    from app.database import get_db
    env = identity
    assert await KnowledgeService().rebuild_index(env.db) == (1, [])
    await Settings.set_embedding_config(env.db, EmbeddingConfig(api_url='https://embed.example/v1', api_key='synthetic-key-A', model='model-B', dimension=2))
    admin = User(username='identity-admin', password_hash='unused', role='admin', is_active=True)
    env.db.add(admin); await env.db.commit()
    app = FastAPI(); app.include_router(router)
    async def db(): yield env.db
    async def user(): return admin
    app.dependency_overrides[get_db] = db; app.dependency_overrides[get_current_user] = user
    async with AsyncClient(transport=ASGITransport(app), base_url='http://test') as client:
        response = await client.get('/api/knowledge/index/status')
    assert response.status_code == 200
    data = response.json()
    assert data['embedding_model'] == 'model-A' and data['current_contract']['model'] == 'model-B'
    assert data['needs_rebuild'] and not data['vector_compatible']
    assert 'synthetic-key' not in response.text
    # A failed generation load may leave zero live items; it still needs repair.
    env.store.clear()
    env.store._last_error = {'type': 'invalid_generation', 'message': 'synthetic damaged generation'}
    async with AsyncClient(transport=ASGITransport(app), base_url='http://test') as client:
        damaged = await client.get('/api/knowledge/index/status')
    assert damaged.status_code == 200
    assert damaged.json()['count'] == 0 and damaged.json()['needs_rebuild']
    assert not damaged.json()['vector_compatible']


@pytest.mark.asyncio
async def test_key_rotation_is_an_opaque_scope_change_and_query_cache_is_bounded(identity):
    env = identity
    assert await KnowledgeService().rebuild_index(env.db) == (1, [])
    before = env.store.get_index_info()['embedding_contract']['credential_scope']
    await Settings.set_embedding_config(env.db, EmbeddingConfig(api_url='https://embed.example/v1', api_key='synthetic-key-B', model='model-A', dimension=2))
    vector = VectorService(); assert await vector.sync_config_from_db(env.db)
    assert vector.contract['credential_scope'] != before and not vector.compatibility()[0]
    for index in range(260): assert await vector.embed_text('query-' + str(index)) == [1.0, 0.0]
    assert len(vector._cache) == 256 and 'query-0' not in vector._cache


@pytest.mark.asyncio
@pytest.mark.parametrize('rows', [
    [{'index': 0, 'embedding': [float('nan'), 0.0]}],
    [{'index': 0, 'embedding': [True, 0.0]}],
    [{'index': 0, 'embedding': [10 ** 400, 0.0]}],
    [{'index': -1, 'embedding': [1.0, 0.0]}],
    [{'index': 0, 'embedding': [1.0, 0.0]}, {'index': 0, 'embedding': [1.0, 0.0]}],
])
async def test_invalid_vectors_are_rejected_without_repeating_provider_calls(identity, monkeypatch, rows):
    env = identity
    calls = []
    def post(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status_code=200, json=lambda: {'data': rows})
    monkeypatch.setattr('app.core.embedding.requests.post', post)
    vector = VectorService(); assert await vector.sync_config_from_db(env.db)
    assert await vector.embed_texts(['one']) is None and len(calls) == 1
