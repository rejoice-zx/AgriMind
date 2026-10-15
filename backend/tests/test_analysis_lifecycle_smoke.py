"""Main lifecycle paths only; synthetic sync sessions and a timer-only event loop."""
import asyncio
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from app.core.model_base import Base
from app.core.analysis_lifecycle import run_owned_analysis
from app.models import User, AgriTurn, AgriConsultation, DiseaseRecord
from app.models.diagnosis_media import DiagnosisRequest
from app.schemas.agent import AgentDecisionRequest, AgentDecisionResponse
from app.schemas.expert import VisualDiagnosisRequest
from app.services.agent_service import AgriAgentService
from app.services.agri_history_service import AgriHistoryService
from app.services.diagnosis_request_service import DiagnosisRequestService
from app.services.diagnosis_media_service import DiagnosisMediaService
from app.services.expert_service import ExpertWorkbenchService
from tests.test_farming_submission_smoke import AsyncAdapter
from tests.test_runtime_boundaries_smoke import run_timers


class OwnedSession(AsyncAdapter):
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.session.close()


def synthetic(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr('sqlalchemy.ext.asyncio.async_sessionmaker',
        lambda bind, **kwargs: lambda: OwnedSession(Session(bind, expire_on_commit=False)))
    return engine


def test_disconnect_keeps_owned_worker_and_same_id_joins_saved_result(monkeypatch):
    engine = synthetic(monkeypatch)
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def model(*args, **kwargs):
            calls.append(1); started.set(); await release.wait()
            return AgentDecisionResponse(query='synthetic', conclusion='synthetic saved result',
                decision_type='GENERAL', confidence=0.1, can_proceed=False)
        monkeypatch.setattr(AgriAgentService, 'decide', model)
        with Session(engine, expire_on_commit=False) as session:
            user = User(username='synthetic-disconnect', password_hash='synthetic', role='customer')
            session.add(user); session.commit()
            adapter = AsyncAdapter(session); adapter.bind = engine
            request = AgentDecisionRequest(request_id=uuid4(), query='synthetic')
            waiter = asyncio.create_task(run_owned_analysis(adapter, request, user, 'agent', AgriHistoryService.decide))
            await started.wait()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError): await waiter
            # Request session can close before the worker finishes.
            session.close()
            joined = asyncio.create_task(run_owned_analysis(adapter, request, user, 'agent', AgriHistoryService.decide))
            release.set()
            result = await joined
            assert result.conclusion == 'synthetic saved result' and calls == [1]
            receipt = await AgriHistoryService.request_status(adapter, request.request_id, user)
            assert receipt.status == 'completed' and receipt.result.turn_id == result.turn_id
            replay = await run_owned_analysis(adapter, request, user, 'agent', AgriHistoryService.decide)
            assert replay.turn_id == result.turn_id and calls == [1]
            assert session.scalar(select(func.count()).select_from(AgriTurn)) == 1
    try: run_timers(scenario())
    finally: engine.dispose()


@pytest.mark.parametrize('kind', ['agent', 'diagnosis'])
def test_explicit_cancel_is_owned_terminal_and_saves_no_success_case(monkeypatch, kind):
    engine = synthetic(monkeypatch)
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()
        async def model(*args, **kwargs):
            started.set()
            try: await asyncio.Event().wait()
            finally: stopped.set()
        async def local_media(*args, **kwargs): return None
        monkeypatch.setattr(DiagnosisMediaService, 'access', local_media)
        monkeypatch.setattr(AgriAgentService, 'decide', model)
        monkeypatch.setattr(ExpertWorkbenchService, 'visual_diagnose', model)
        with Session(engine, expire_on_commit=False) as session:
            user = User(username='synthetic-cancel', password_hash='synthetic', role='customer')
            stranger = User(username='synthetic-stranger', password_hash='synthetic', role='customer')
            session.add_all([user, stranger]); session.commit()
            adapter = AsyncAdapter(session); adapter.bind = engine
            request = AgentDecisionRequest(request_id=uuid4(), query='synthetic') if kind == 'agent' else VisualDiagnosisRequest(request_id=uuid4(), image_url='synthetic-local')
            handler = AgriHistoryService.decide if kind == 'agent' else DiagnosisRequestService.run
            cancel = AgriHistoryService.cancel_request if kind == 'agent' else DiagnosisRequestService.cancel
            task = asyncio.create_task(run_owned_analysis(adapter, request, user, kind, handler))
            await started.wait()
            with pytest.raises(HTTPException) as forbidden:
                await cancel(adapter, request.request_id, stranger)
            assert forbidden.value.status_code == 404
            assert not stopped.is_set()
            receipt = await cancel(adapter, request.request_id, user)
            assert receipt.status == 'failed'
            with pytest.raises(HTTPException) as cancelled: await task
            assert cancelled.value.status_code == 409 and stopped.is_set()
            repeated = await cancel(adapter, request.request_id, user)
            assert repeated.status == 'failed'
            if kind == 'agent':
                assert '取消' in repeated.message
            else:
                assert repeated.result.error_code == 'cancelled'
                assert session.get(DiagnosisRequest, str(request.request_id)).record_id is None
            assert session.scalar(select(func.count()).select_from(AgriConsultation)) == 0
            assert session.scalar(select(func.count()).select_from(DiseaseRecord)) == 0
    try: run_timers(scenario())
    finally: engine.dispose()
