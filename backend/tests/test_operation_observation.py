"""Real SQL/ASGI and controlled model transport tests, without deployment services."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from app.core.operation_observation import (observed_model, set_operation_outcome,
    request_observation, RequestObservation, observe_operation)
from app.database import create_database_engine
from app.middleware.agri_performance import AgriPerformanceMiddleware
from app.services.performance_service import PerformanceService, MetricCollector
from app.services import performance_service as performance_module
from app.services.agent_service import AgriAgentService
from app.api.performance import router as performance_router
from app.api.dependencies import get_current_user
from app.models.user import User


@pytest.fixture
def monitor(monkeypatch):
    monkeypatch.setattr(PerformanceService, '_instance', None)
    instance = PerformanceService()
    monkeypatch.setattr(performance_module, '_performance_service', instance)
    return instance


def test_classifications_keep_recent_and_lifetime_windows_separate(monitor):
    for index in range(1005):
        monitor.record('agri_decision_request', 0, True, outcome='degraded' if index == 0 else 'success')
    summary, samples = monitor.snapshot(last_n=2)
    stat = samples['agri_decision_request']
    assert stat.count == 2 and stat.outcome_counts == {'success': 2}
    assert stat.total_outcome_counts == {'degraded': 1, 'success': 1004}
    assert summary['total_requests'] == 1005 and summary['overall_success_rate'] == 1
    assert summary['degraded_requests'] == 1 and summary['process_id'] == os.getpid()
    assert len(monitor.get_collector('agri_decision_request')._records) == 1000
    monitor.clear_all()
    assert monitor.snapshot()[1]['agri_decision_request'].total_outcome_counts == {}
    assert monitor.get_summary()['degraded_requests'] == 0
    with pytest.raises(ValueError): monitor.record('agri_decision_request', 1, outcome='payload-derived-label')


@pytest.mark.asyncio
async def test_concurrent_request_scopes_do_not_share_degradation(monitor):
    app = FastAPI(); app.add_middleware(AgriPerformanceMiddleware, service=monitor)
    entered = asyncio.Event(); release = asyncio.Event()
    @observed_model('agri_decision_model')
    async def model(degrade):
        if degrade:
            entered.set(); await release.wait()
        else:
            await entered.wait(); set_operation_outcome('success'); release.set()
        return {'answer': 'synthetic result'}
    @app.get('/api/agent/decide')
    async def endpoint(degrade: bool): return await model(degrade)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        replies = await asyncio.gather(client.get('/api/agent/decide?degrade=true'), client.get('/api/agent/decide?degrade=false'))
    assert all(reply.status_code == 200 for reply in replies)
    summary, stats = monitor.snapshot()
    assert summary['total_requests'] == 2 and summary['overall_success_rate'] == 1
    assert summary['degraded_requests'] == 1
    assert stats['agri_decision_request'].outcome_counts == {'degraded': 1, 'success': 1}
    assert stats['agri_decision_model'].success_rate == 0.5
    assert request_observation.get() is None


@pytest.mark.asyncio
async def test_real_cursor_errors_are_counted_without_sql_or_parameters(tmp_path, monitor):
    engine = create_database_engine('sqlite+aiosqlite:///' + (tmp_path / 'observed.db').as_posix())
    app = FastAPI(); app.add_middleware(AgriPerformanceMiddleware, service=monitor)
    async def query():
        async with engine.connect() as connection:
            await connection.execute(text('SELECT :private_value'), {'private_value': 'synthetic-sensitive-value'})
            try:
                await connection.execute(text('SELECT * FROM deliberately_missing_table'))
            except Exception:
                pass
        return {'ok': True}
    app.add_api_route('/api/farms', query); app.add_api_route('/outside-scope', query)
    try:
        async with engine.connect() as connection: await connection.execute(text('SELECT 1'))
        assert monitor.get_collector('agri_database_statement').get_stats().count == 0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            assert (await client.get('/api/farms')).status_code == 200
            assert (await client.get('/outside-scope')).status_code == 200
        stat = monitor.get_collector('agri_database_statement').get_stats()
        assert stat.count == 2 and stat.outcome_counts == {'success': 1, 'error': 1}
        assert stat.min_duration >= 0 and stat.max_duration > 0
        assert monitor.get_summary()['total_requests'] == 1
        for record in monitor.get_collector('agri_database_statement')._records:
            assert record.metadata == {} and 'SELECT' not in repr(record) and 'sensitive' not in repr(record)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_incomplete_response_is_not_success_and_context_is_reset(monitor):
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        raise asyncio.CancelledError()
    observed = AgriPerformanceMiddleware(app, service=monitor)
    async def noop(*args): pass
    with pytest.raises(asyncio.CancelledError):
        await observed({'type': 'http', 'path': '/api/agent/decide'}, noop, noop)
    stat = monitor.get_collector('agri_decision_request').get_stats()
    assert stat.success_rate == 0 and stat.outcome_counts == {'cancelled': 1}
    assert request_observation.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('body,status,expected', [
    ({'choices': [{'message': {'content': '{"can_proceed": false, "need_expert": true}'}}]}, 200, 'success'),
    ({'choices': [{'message': {'content': 'not-json'}}]}, 200, 'degraded'),
    ({'error': {'message': 'synthetic-sensitive-upstream-error'}}, 503, 'degraded'),
])
async def test_transport_completion_is_distinct_from_model_json_result(monkeypatch, monitor, body, status, expected):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **options: real_client(transport=transport, **options))
    result = await AgriAgentService._synthesize_with_llm('synthetic question', '', '', None, '', [], None, None,
        api_key='synthetic-key', base_url='https://model.example/v1', model_name='synthetic-model')
    assert isinstance(result, dict) and result['can_proceed'] is False
    stats = monitor.get_all_stats()
    assert stats['agri_decision_model'].outcome_counts == {expected: 1}
    assert stats['agri_decision_model_http'].outcome_counts == {'success' if status == 200 else 'error': 1}
    assert monitor.get_summary()['total_requests'] == 0
    assert 'synthetic-sensitive-upstream-error' not in monitor.export_report()


@pytest.mark.asyncio
async def test_offline_model_has_no_http_sample_and_sampling_failure_preserves_result(monitor, monkeypatch):
    args = ('question', '', '', None, '', [], None, None)
    result = await AgriAgentService._synthesize_with_llm(*args, api_key='')
    assert result['can_proceed'] is False
    assert monitor.get_collector('agri_decision_model').get_stats().outcome_counts == {'degraded': 1}
    assert monitor.get_collector('agri_decision_model_http').get_stats().count == 0
    def broken(*args, **kwargs): raise RuntimeError('synthetic observer unavailable')
    monkeypatch.setattr(monitor, 'record', broken)
    assert await AgriAgentService._synthesize_with_llm(*args, api_key='') == result


@pytest.mark.asyncio
async def test_metrics_api_returns_classifications_and_remains_admin_only(monitor):
    app = FastAPI(); app.include_router(performance_router)
    user = User(id=987, username='synthetic-admin', password_hash='unused', role='admin', is_active=True)
    async def identity(): return user
    app.dependency_overrides[get_current_user] = identity
    monitor.record('agri_decision_request', 0.1, True, outcome='degraded')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        for endpoint in ('summary', 'metrics'):
            response = await client.get('/api/performance/' + endpoint)
            assert response.status_code == 200
            data = response.json(); summary = data.get('summary', data)
            assert summary['degraded_requests'] == 1 and summary['process_id'] == os.getpid()
            if endpoint == 'metrics': assert data['metrics']['agri_decision_request']['outcome_counts'] == {'degraded': 1}
        user.role = 'customer'
        assert (await client.get('/api/performance/metrics')).status_code == 403


@pytest.mark.asyncio
async def test_completed_request_scope_excludes_late_background_sql(tmp_path, monitor):
    engine = create_database_engine('sqlite+aiosqlite:///' + (tmp_path / 'late.db').as_posix())
    release = asyncio.Event(); tasks = []
    async def worker():
        await release.wait()
        async with engine.connect() as connection: await connection.execute(text('SELECT 1'))
    app = FastAPI(); app.add_middleware(AgriPerformanceMiddleware, service=monitor)
    @app.get('/api/farms')
    async def endpoint(): tasks.append(asyncio.create_task(worker())); return {'ok': True}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            await client.get('/api/farms')
        release.set(); await asyncio.gather(*tasks)
        assert monitor.get_collector('agri_database_statement').get_stats().count == 0
    finally:
        release.set()
        await engine.dispose()


def test_fresh_active_router_import_does_not_activate_retired_services(tmp_path):
    code = r"""
import dotenv
from pydantic_settings import BaseSettings
dotenv.load_dotenv = lambda *args, **kwargs: False
original = BaseSettings.__init__
def isolated(self, *args, **kwargs):
    kwargs['_env_file'] = None
    original(self, *args, **kwargs)
BaseSettings.__init__ = isolated
import app.api
import sys
for module in ('chat_service', 'llm_service', 'human_service', 'product_service', 'statistics_service'):
    assert 'app.services.' + module not in sys.modules, module
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE='1')
    completed = subprocess.run([sys.executable, '-c', code], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_embedding_worker_propagates_request_scope_and_separates_bad_vectors(monkeypatch, monitor, controlled_dns):
    from types import SimpleNamespace
    from app.core.embedding import EmbeddingClient
    from app.services.vector_service import VectorService
    controlled_dns({'embedding.example': ['93.184.216.34']})
    state = {'bad': False}
    def post(*args, **kwargs):
        assert request_observation.get() is not None  # Actual executor worker received the scope.
        values = [1.0] if state['bad'] else [1.0, 0.0]
        return SimpleNamespace(status_code=200, json=lambda: {'data': [{'index': 0, 'embedding': values}]})
    monkeypatch.setattr('app.core.embedding.requests.post', post)
    embedding_client = object.__new__(EmbeddingClient); embedding_client._initialized = False; EmbeddingClient.__init__(embedding_client)
    embedding_client.configure(api_key='synthetic-key', api_url='https://embedding.example/v1', model='synthetic-model', dimension=2)
    app = FastAPI(); app.add_middleware(AgriPerformanceMiddleware, service=monitor)
    @app.get('/api/agent/decide')
    async def endpoint(bad: bool):
        state['bad'] = bad
        vector = VectorService(); vector._bound_client = embedding_client.snapshot()
        result = await vector.embed_text('synthetic question')
        return {'available': result is not None}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        assert (await client.get('/api/agent/decide?bad=false')).json()['available']
        assert not (await client.get('/api/agent/decide?bad=true')).json()['available']
    stats = monitor.get_all_stats()
    assert stats['embedding_http'].outcome_counts == {'success': 2}
    assert stats['embedding_api'].outcome_counts == {'success': 1, 'degraded': 1}
    assert monitor.get_summary()['degraded_requests'] == 1
    assert monitor.get_summary()['total_requests'] == 2


from tests.test_vision_result_integrity import vision_mock


@pytest.mark.asyncio
@pytest.mark.parametrize('content,expected', [('{"suspected_disease":"待鉴别", "confidence":null}', 'success'), ('not-json', 'degraded')])
async def test_vision_json_result_is_not_equivalent_to_http_200(vision_mock, monitor, content, expected):
    from app.services.vision_service import VisionService
    vision_mock['content'] = content
    result = await VisionService.diagnose_leaf_image('/static/uploads/input.png', api_key='synthetic-key')
    assert result['need_expert']  # Human review can be a valid parsed model result.
    stats = monitor.get_all_stats()
    assert stats['agri_vision_model_http'].outcome_counts == {'success': 1}
    assert stats['agri_vision_model'].outcome_counts == {expected: 1}
