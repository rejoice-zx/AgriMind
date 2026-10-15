"""Statistics and telemetry regressions: real records, SQLite boundaries and ASGI traffic."""
import asyncio
import math
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.models import (User, Farm, Field, CropPlanting, FarmingRecord, DiseaseRecord, KnowledgeItem,
    Product, AgriConversation, AgriTurn, AgriConsultation)
from tests.legacy_fixtures import LegacyRows
from app.services.agri_statistics_service import AgriStatisticsService
from app.services import agri_statistics_service as statistics_module
from app.services import performance_service as performance_module
from app.services.performance_service import PerformanceService, MetricCollector
from app.services.agent_service import AgriToolRegistry
from app.middleware.agri_performance import AgriPerformanceMiddleware, agricultural_metric

NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
START = datetime(2026, 10, 8, 16, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def domain_data(test_session, monkeypatch):
    db = test_session
    monkeypatch.setattr(statistics_module, 'utcnow', lambda: NOW)
    owner = User(username='actual_owner', password_hash='fictional', role='customer', is_active=True)
    db.add(owner); await db.flush()
    farm = Farm(name='Actual farm', owner_id=owner.id)
    db.add(farm); await db.flush()
    field = Field(name='Actual field', farm_id=farm.id, area=2)
    db.add(field); await db.flush()
    db.add_all([CropPlanting(field_id=field.id, crop='黄瓜', status=state, planting_date=date(2026, 9, 1))
        for state in ['active', 'active', 'harvested']])
    db.add_all([FarmingRecord(field_id=field.id, record_type='施肥', record_date=date(2026, 10, 9),
        created_at=datetime(2026, 10, 7, tzinfo=timezone.utc)),
        FarmingRecord(field_id=field.id, record_type='灌溉', record_date=date(2026, 10, 8), created_at=NOW)])
    db.add(DiseaseRecord(field_id=field.id, disease_name='未确定', status='pending_review'))
    db.add_all([KnowledgeItem(id='current1', question='a', answer='b', category='当前', is_current=1),
                KnowledgeItem(id='current2', question='a2', answer='b2', category='当前', is_current=1),
                KnowledgeItem(id='obsolete', question='old', answer='old', category='旧版', is_current=0)])
    db.add(Product(id='legacy-product', name='Legacy product', price=1, category='old'))
    conversation = AgriConversation(id=str(uuid4()), owner_id=owner.id, field_id=field.id,
        field_ref=field.id, title='Actual agriculture', created_at=START, updated_at=NOW)
    db.add(conversation); await db.flush()
    turns = []
    for state, created, duration, query in [
        ('completed', START, 1001, 'same question'),
        ('completed', datetime(2026, 10, 8, 23, tzinfo=timezone.utc), 0, ' same question '),
        ('failed', datetime(2026, 10, 9, 4, tzinfo=timezone.utc), None, 'failed question'),
        ('pending', datetime(2026, 10, 9, 9, 50, tzinfo=timezone.utc), None, 'running question'),
        ('pending', datetime(2026, 10, 9, 5, tzinfo=timezone.utc), None, 'stale question'),
        ('completed', datetime(2026, 10, 8, 15, 59, tzinfo=timezone.utc), 9999, 'before window'),
        ('completed', datetime(2026, 10, 9, 16, tzinfo=timezone.utc), 99, 'next business day'),
    ]:
        turn = AgriTurn(id=str(uuid4()), conversation_id=conversation.id, request_id=str(uuid4()),
            request_payload={'query': query}, query=query, status=state, created_at=created, updated_at=NOW,
            decision={'total_duration_ms': duration, 'decision_type': 'GENERAL'} if state == 'completed' else None)
        db.add(turn); turns.append(turn)
    await db.flush()
    db.add_all([AgriConsultation(turn_id=turns[0].id, status='pending', created_at=START, updated_at=START),
        AgriConsultation(turn_id=turns[1].id, status='resolved', answer='Actual answer', created_at=START, updated_at=NOW),
        AgriConsultation(turn_id=turns[5].id, status='resolved', answer='Older question answered today',
            created_at=turns[5].created_at, updated_at=NOW)])
    await db.commit()
    await LegacyRows.create_conversation(db, title='Legacy ecommerce', customer_id='visitor_old')
    yield SimpleNamespace(db=db, turns=turns, conversation=conversation)


async def day_snapshot(db):
    return await AgriStatisticsService.snapshot(db, date(2026, 10, 9), date(2026, 10, 9))


@pytest.mark.asyncio
async def test_domain_statistics_exclude_legacy_and_use_actual_agricultural_records(domain_data):
    snapshot = await day_snapshot(domain_data.db)
    o = snapshot.overview
    assert (o.total_farms, o.total_fields, o.active_crop_plantings, o.total_records) == (1, 1, 2, 2)
    assert o.total_conversations == 1 and o.current_knowledge_items == 2
    assert o.total_disease_records == 1 and o.pending_disease_cases == 1
    assert (o.period_turns, o.completed_turns, o.failed_turns, o.pending_turns, o.stale_pending_turns) == (5, 2, 1, 2, 1)
    assert o.completion_rate == pytest.approx(2 / 3)
    assert o.avg_response_time_ms == 500.5 and o.response_time_sample_count == 2
    assert (o.period_consultations, o.pending_consultations, o.resolved_consultations) == (2, 1, 1)
    assert snapshot.categories.knowledge == {'当前': 2}
    assert snapshot.categories.active_crops == {'黄瓜': 2}
    assert snapshot.categories.decisions == {'GENERAL': 2}
    assert snapshot.categories.farming_records == {'施肥': 1}
    assert snapshot.top_questions[0].question == 'same question' and snapshot.top_questions[0].count == 2


@pytest.mark.asyncio
async def test_daily_business_midnight_and_operation_date_semantics(domain_data):
    snapshot = await day_snapshot(domain_data.db)
    day = snapshot.daily[0]
    assert day.date == date(2026, 10, 9)
    assert (day.turns, day.completed, day.failed, day.pending) == (5, 2, 1, 2)
    assert day.conversations == 1
    assert day.farming_records == 1  # Operation date, not entry creation timestamp.
    assert day.consultations_created == 2
    assert day.consultations_resolved == 2  # One consultation was created before this date.


@pytest.mark.asyncio
async def test_statistics_reads_do_not_expire_or_mutate_stale_turns(domain_data):
    env = domain_data
    before = [(turn.id, turn.status, turn.updated_at) for turn in env.turns]
    first = await day_snapshot(env.db)
    second = await day_snapshot(env.db)
    assert first == second
    after = [(turn.id, turn.status, turn.updated_at) for turn in (await env.db.scalars(select(AgriTurn))).all()]
    assert sorted(after) == sorted(before)
    from app.models.conversation import Conversation
    assert await env.db.scalar(select(func.count(Conversation.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [None, True, '100', -1, float('nan'), float('inf')])
async def test_invalid_latency_is_excluded_without_faking_zero(domain_data, invalid):
    for turn in domain_data.turns[:2]:
        turn.decision = {'total_duration_ms': invalid, 'decision_type': 'GENERAL'}
    await domain_data.db.commit()
    result = await day_snapshot(domain_data.db)
    assert result.overview.response_time_sample_count == 0
    assert result.overview.avg_response_time_ms is None
    assert result.overview.completed_turns == 2


@pytest.mark.asyncio
async def test_empty_window_uses_real_zero_counts_and_null_rates(domain_data):
    result = await AgriStatisticsService.snapshot(domain_data.db, date(2026, 8, 1), date(2026, 8, 3))
    assert len(result.daily) == 3
    assert all(item.turns == 0 and item.farming_records == 0 for item in result.daily)
    assert result.overview.completion_rate is None and result.overview.avg_response_time_ms is None
    assert result.overview.total_fields == 1  # Asset counts are current, not date-filtered.
    assert result.top_questions == []


@pytest.mark.asyncio
async def test_statistics_survive_new_session_and_ignore_legacy_counters(domain_data):
    from app.models.statistics import StatisticsDaily
    domain_data.db.add(StatisticsDaily(date=date(2026, 10, 9), conversations=100000, messages=100000))
    await domain_data.db.commit()
    async with async_sessionmaker(domain_data.db.bind, expire_on_commit=False)() as other:
        result = await day_snapshot(other)
    assert result.overview.period_turns == 5
    assert result.top_questions[0].question == 'same question'


@pytest.mark.asyncio
async def test_statistics_extreme_dates_and_default_timezone(domain_data):
    result = await AgriStatisticsService.snapshot(domain_data.db, days=1)
    assert result.start_date == result.end_date == date(2026, 10, 9)
    for day in (date.min, date.max):
        empty = await AgriStatisticsService.snapshot(domain_data.db, day, day)
        assert len(empty.daily) == 1 and empty.daily[0].turns == 0


@pytest.mark.asyncio
async def test_additive_index_migration_handles_existing_tables_without_touching_records(tmp_path):
    from app.core.agri_statistics_indexes import ensure_agri_statistics_indexes, STATISTICS_INDEXES
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old-indexes.db').as_posix()}")
    try:
        async with engine.begin() as connection:
            # Stand-ins for existing tables before this additive index migration.
            for table, columns in [('agri_conversations', 'id TEXT, created_at DATETIME'),
                ('agri_turns', 'id TEXT, created_at DATETIME, status TEXT'),
                ('agri_consultations', 'id TEXT, created_at DATETIME, updated_at DATETIME, status TEXT'),
                ('farming_records', 'id INTEGER, record_date DATE')]:
                await connection.execute(text(f'CREATE TABLE {table} ({columns})'))
            await connection.execute(text("INSERT INTO agri_turns VALUES ('existing', '2026-10-09 00:00:00', 'completed')"))
            await ensure_agri_statistics_indexes(connection)
            await ensure_agri_statistics_indexes(connection)
            names = set((await connection.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))).scalars())
            assert {item[0] for item in STATISTICS_INDEXES} <= names
            assert (await connection.execute(text('SELECT id, status FROM agri_turns'))).one() == ('existing', 'completed')
            plan = (await connection.execute(text("EXPLAIN QUERY PLAN SELECT status, count(*) FROM agri_turns WHERE created_at >= '2026-10-09' GROUP BY status"))).all()
            assert any('ix_agri_turns_created_status' in str(row) for row in plan)
    finally:
        await engine.dispose()


@pytest.fixture
def monitor(monkeypatch):
    monkeypatch.setattr(PerformanceService, '_instance', None)
    instance = PerformanceService()
    monkeypatch.setattr(performance_module, '_performance_service', instance)
    return instance


def test_telemetry_cumulative_counts_exclude_nested_calls_and_preserve_zero_rate(monitor):
    for index in range(1500):
        monitor.record('agri_decision_request', index + 1, index % 2 == 0)
        monitor.record('agri_tool_get_weather', 0.1, True)
    monitor.record('chat_api', 1, True)
    monitor.record('product_add', 1, True)
    summary, stats = monitor.snapshot(last_n=100)
    assert summary['total_requests'] == 1500 and summary['overall_success_rate'] == 0.5
    assert stats['agri_decision_request'].count == 100
    assert stats['agri_decision_request'].total_count == 1500
    assert stats['agri_decision_request'].p50_duration == 1450
    assert stats['agri_decision_request'].p95_duration == 1495
    assert stats['agri_decision_request'].p99_duration == 1499
    assert 'chat_api' not in stats and 'product_add' not in stats
    original_start = summary['start_time']
    monitor.clear_all()
    assert monitor.get_summary()['start_time'] == original_start
    assert monitor.get_summary()['overall_success_rate'] is None
    assert monitor.get_summary()['cleared_at'] is not None
    monitor.record('agri_decision_request', 0, False)
    assert monitor.get_summary()['overall_success_rate'] == 0
    assert '0.0%' in monitor.export_report()


@pytest.mark.parametrize('invalid', [True, -1, float('nan'), float('inf')])
def test_metric_collector_rejects_invalid_measurements(invalid):
    collector = MetricCollector('test')
    with pytest.raises(ValueError): collector.record(invalid)
    assert collector.get_stats().count == 0


def test_percentiles_small_sample_and_finite_mean():
    collector = MetricCollector('test')
    assert collector.get_stats().success_rate is None
    collector.record(1)
    collector.record(2, False)
    stats = collector.get_stats()
    assert (stats.p50_duration, stats.p95_duration, stats.p99_duration) == (1, 2, 2)
    large = MetricCollector('large')
    large.record(1e308); large.record(1e308)
    assert math.isfinite(large.get_stats().avg_duration)


def test_concurrent_metrics_and_snapshot_counts_are_safe(monitor):
    def write(index):
        for _ in range(100):
            monitor.record('agri_field_request', 0.01)
            monitor.get_collector('private_test_' + str(index))
            monitor.snapshot()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write, range(4)))
    assert monitor.get_summary()['total_requests'] == 400


@pytest.mark.asyncio
async def test_actual_asgi_requests_count_once_without_monitor_observer_or_payload(monitor):
    app = FastAPI()
    app.add_middleware(AgriPerformanceMiddleware, service=monitor)
    @app.post('/api/agent/decide')
    async def decide():
        await AgriToolRegistry.execute_tool('calculate_gdd', {'t_max': 20, 't_min': 10}, None)
        return {'can_proceed': False}
    @app.get('/api/fields/invalid')
    async def invalid(): raise HTTPException(422, 'invalid resource')
    @app.get('/api/fields/error')
    async def error(): raise RuntimeError('controlled failure')
    @app.get('/api/performance/summary')
    async def read(): return monitor.get_summary()
    async with AsyncClient(transport=ASGITransport(app, raise_app_exceptions=False), base_url='http://test') as client:
        assert (await client.post('/api/agent/decide', json={'query': 'private question'})).status_code == 200
        assert (await client.get('/api/fields/invalid')).status_code == 422
        assert (await client.get('/api/fields/error')).status_code == 500
        await client.get('/api/performance/summary')
    summary = monitor.get_summary()
    assert summary['total_requests'] == 3 and summary['overall_success_rate'] == pytest.approx(1 / 3)
    assert monitor.get_collector('agri_tool_calculate_gdd').get_stats().count == 1
    # The outer server error handler sends 500 after telemetry unwinds; do not invent an observed status.
    assert [record.metadata['status_code'] for record in monitor.get_collector('agri_field_request')._records] == [422, None]
    for collector in monitor._collectors.values():
        assert all(set(record.metadata) <= {'status_code'} for record in collector._records)
    assert agricultural_metric('/api/farms-unrelated') is None
    assert agricultural_metric('/api/chat/conversations') is None


@pytest.mark.asyncio
async def test_cancelled_response_is_not_a_successful_measurement(monitor):
    async def cancelled(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        raise asyncio.CancelledError()
    async def receive(): return {'type': 'http.request', 'body': b''}
    async def send(message): pass
    with pytest.raises(asyncio.CancelledError):
        await AgriPerformanceMiddleware(cancelled, monitor)({'type': 'http', 'path': '/api/agent/decide'}, receive, send)
    assert monitor.get_summary()['total_requests'] == 1
    assert monitor.get_summary()['overall_success_rate'] == 0


@pytest.mark.asyncio
async def test_post_response_failure_does_not_relabel_an_already_complete_2xx_response(monitor):
    async def response(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'complete'})
        raise RuntimeError('controlled background failure after delivery')
    async def receive(): return {'type': 'http.request', 'body': b''}
    async def send(message): pass
    with pytest.raises(RuntimeError):
        await AgriPerformanceMiddleware(response, monitor)({'type': 'http', 'path': '/api/agent/decide'}, receive, send)
    assert monitor.get_summary()['overall_success_rate'] == 1


@pytest.mark.asyncio
async def test_main_middleware_and_monitor_api_use_matching_cumulative_schema(test_session, monitor):
    from app.main import app
    from app.database import get_db
    from app.api.dependencies import get_current_user
    admin = User(username='observe_admin', password_hash='fictional', role='admin', is_active=True)
    test_session.add(admin); await test_session.commit()
    async def database(): yield test_session
    async def current_user(): return admin
    original = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = current_user
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url='http://testserver') as client:
            assert (await client.post('/api/agent/tools/execute', json={
                'tool_name': 'calculate_gdd', 'arguments': {'t_max': 20, 't_min': 10}})).status_code == 200
            assert (await client.post('/api/agent/tools/execute', json={})).status_code == 422
            response = await client.get('/api/performance/metrics')
            assert response.status_code == 200
            data = response.json()
            assert data['summary']['total_requests'] == 2
            assert data['summary']['overall_success_rate'] == 0.5
            assert data['summary']['storage_scope'] == 'current_process'
            assert data['metrics']['agri_tool_calculate_gdd']['count'] == 1
            monitor.clear_all()
            assert (await client.get('/api/performance/summary')).json()['overall_success_rate'] is None
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


@pytest.mark.asyncio
async def test_broken_telemetry_does_not_change_business_response():
    received = []
    async def response(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'actual response'})
    async def receive(): return {'type': 'http.request', 'body': b''}
    async def send(message): received.append(message)
    def broken(*args): raise RuntimeError('controlled telemetry failure')
    await AgriPerformanceMiddleware(response, SimpleNamespace(record=broken))(
        {'type': 'http', 'path': '/api/agent/decide'}, receive, send)
    assert received[-1]['body'] == b'actual response'
