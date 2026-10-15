"""Focused local deadline, history and readiness contracts, using synthetic storage only."""
import asyncio
import selectors
import time
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, func, MetaData, Table, Column, Integer
from sqlalchemy.orm import Session
from app.database import Base
from app.models import User, AgriTurn, AgriConsultation, DiagnosisRequest, DiseaseRecord
from app.schemas.agent import AgentDecisionRequest
from app.schemas.expert import VisualDiagnosisRequest
from app.services.agent_service import AgriAgentService
from app.services.agri_history_service import AgriHistoryService
from app.services.expert_service import ExpertWorkbenchService
from app.services.diagnosis_request_service import DiagnosisRequestService
from app.services.diagnosis_media_service import DiagnosisMediaService
from app.core.inference_budget import INFERENCE_SECONDS
from app.core.schema_revision import record_schema_revision, verify_runtime_schema, CURRENT_SCHEMA_REVISION
from app.core.storage_readiness import storage_readiness
from tests.test_farming_submission_smoke import AsyncAdapter


class TimerSelector(selectors.BaseSelector):
    """These synthetic service adapters use no async I/O; schedule tasks/timers only."""
    def select(self, timeout=None):
        time.sleep(min(max(timeout or 0, 0), 0.01))
        return []
    def get_map(self): return {}
    def register(self, *args): raise AssertionError('No I/O is allowed in the timer-only smoke check')
    def unregister(self, *args): raise AssertionError('No I/O is allowed in the timer-only smoke check')


class TimerLoop(asyncio.SelectorEventLoop):
    # Default Windows loop initialization uses local sockets, unavailable in this sandbox.
    def __init__(self): super().__init__(TimerSelector())
    def _make_self_pipe(self): pass
    def _close_self_pipe(self): pass
    def _write_to_self(self): pass


def run_timers(coroutine):
    with asyncio.Runner(loop_factory=TimerLoop) as runner:
        return runner.run(coroutine)


def test_agent_total_deadline_cancels_and_marks_one_failed_turn(monkeypatch):
    state = {'calls': 0, 'cancelled': False}
    async def slow(*args, **kwargs):
        state['calls'] += 1
        try:
            await asyncio.Event().wait()
        finally:
            state['cancelled'] = True
    monkeypatch.setitem(INFERENCE_SECONDS, 'agent', 0.02)
    monkeypatch.setattr(AgriAgentService, 'decide', slow)
    engine = create_engine('sqlite:///:memory:')
    try:
        Base.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as session:
            user = User(username='synthetic-deadline-owner', password_hash='synthetic', role='customer')
            session.add(user); session.commit()
            request = AgentDecisionRequest(request_id=uuid4(), query='synthetic general consultation')
            adapter = AsyncAdapter(session)
            with pytest.raises(HTTPException) as timeout:
                run_timers(AgriHistoryService.decide(adapter, request, user))
            assert timeout.value.status_code == 504
            row = session.scalar(select(AgriTurn).where(AgriTurn.request_id == str(request.request_id)))
            assert row.status == 'failed' and row.active_conversation_id is None and '处理时限' in row.error
            assert session.scalar(select(func.count()).select_from(AgriConsultation)) == 0
            with pytest.raises(HTTPException) as retry:
                run_timers(AgriHistoryService.decide(adapter, request, user))
            assert retry.value.status_code == 409
            assert state == {'calls': 1, 'cancelled': True}
    finally:
        engine.dispose()


def test_diagnosis_total_deadline_keeps_failed_receipt_and_no_case(monkeypatch):
    state = {'cancelled': False}
    async def access(*args, **kwargs): return None
    async def slow(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            state['cancelled'] = True
    monkeypatch.setitem(INFERENCE_SECONDS, 'diagnosis', 0.02)
    monkeypatch.setattr(DiagnosisMediaService, 'access', access)
    monkeypatch.setattr(ExpertWorkbenchService, 'visual_diagnose', slow)
    engine = create_engine('sqlite:///:memory:')
    try:
        Base.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as session:
            user = User(username='synthetic-diagnosis-deadline', password_hash='synthetic', role='customer')
            session.add(user); session.commit()
            request = VisualDiagnosisRequest(request_id=uuid4(), image_url='synthetic-local-reference')
            with pytest.raises(HTTPException) as timeout:
                run_timers(DiagnosisRequestService.run(AsyncAdapter(session), request, user))
            assert timeout.value.status_code == 504 and state['cancelled']
            row = session.get(DiagnosisRequest, str(request.request_id))
            assert row.status == 'failed' and row.result is None and row.record_id is None
            assert session.scalar(select(func.count()).select_from(DiseaseRecord)) == 0
    finally:
        engine.dispose()


def test_runtime_schema_requires_current_ledger_and_matching_structure():
    engine = create_engine('sqlite:///:memory:')
    metadata = MetaData()
    Table('synthetic', metadata, Column('id', Integer, primary_key=True))
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            with pytest.raises(ValueError, match='尚未升级'):
                verify_runtime_schema(connection, metadata)
            connection.exec_driver_sql('PRAGMA user_version=' + str(CURRENT_SCHEMA_REVISION))
            with pytest.raises(ValueError, match='检查点'):
                verify_runtime_schema(connection, metadata)
            record_schema_revision(connection, metadata)
            verify_runtime_schema(connection, metadata)
            connection.exec_driver_sql('CREATE TABLE unexpected_table(id INTEGER)')
            with pytest.raises(ValueError, match='结构与迁移账本不一致'):
                verify_runtime_schema(connection, metadata)
    finally:
        engine.dispose()


def test_storage_probe_is_temporary_and_missing_paths_stay_missing(tmp_path):
    good = tmp_path / 'writable'; good.mkdir()
    missing = tmp_path / 'missing'
    result = storage_readiness({'data': good, 'logs': missing})
    assert result == {'data': 'writable', 'logs': 'missing'}
    assert list(good.iterdir()) == [] and not missing.exists()


def test_ready_endpoint_rejects_old_schema_and_accepts_checked_local_storage(tmp_path, monkeypatch):
    import json
    from app.main import readiness_check
    from app.config import settings
    from app.models import KnowledgeItem
    from tests.test_farming_submission_smoke import finish_immediate
    data, logs = tmp_path / 'data', tmp_path / 'logs'
    data.mkdir(exist_ok=True); logs.mkdir()
    prior_data = set(data.iterdir())
    monkeypatch.setattr(settings, 'DATA_DIR', str(data))
    monkeypatch.setattr(settings, 'LOGS_DIR', str(logs))
    async def inline(function, *args, **kwargs): return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', inline)
    class ConnectionAdapter:
        def __init__(self, connection):
            self.connection = connection
            self.dialect = connection.dialect
        async def run_sync(self, function): return function(self.connection)
    class ReadyAdapter(AsyncAdapter):
        async def connection(self): return ConnectionAdapter(self.session.connection())
        async def scalars(self, query): return self.session.scalars(query)
    engine = create_engine('sqlite:///:memory:')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            adapter = ReadyAdapter(session)
            old = finish_immediate(readiness_check(adapter))
            assert old.status_code == 503 and json.loads(old.body)['database'] == 'unavailable_or_incompatible'
            session.add(KnowledgeItem(id='synthetic-ready-knowledge', question='synthetic', answer='synthetic', crop='番茄', source_type='expert', authority_level='未核验'))
            session.commit()
            record_schema_revision(session.connection(), Base.metadata)
            ready = finish_immediate(readiness_check(adapter))
            body = json.loads(ready.body)
            assert ready.status_code == 200 and body['schema_revision'] == CURRENT_SCHEMA_REVISION
            assert body['storage'] == {'data': 'writable', 'logs': 'writable'}
            assert body['external_providers'] == 'not_probed'
            assert list(logs.iterdir()) == [] and set(data.iterdir()) == prior_data
    finally:
        engine.dispose()
