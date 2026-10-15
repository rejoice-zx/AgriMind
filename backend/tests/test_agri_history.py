"""Archive/consultation integration with real FK constraints, no inference or external I/O."""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient, ASGITransport
from sqlalchemy import event, select, func, delete, update, inspect
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import Base, get_db
from app.api.dependencies import get_current_user
from app.api.agri_agent import router as agent_router
from app.api.agri_history import router as history_router
from app.api.users import update_user, UpdateUserRequest, CreateUserRequest
from app.models.user import User
from app.models.farm import Farm
from app.models.field import Field
from app.models.disease_record import DiseaseRecord
from app.models.agri_conversation import AgriConversation, AgriTurn, AgriConsultation
from app.schemas.agent import AgentDecisionRequest, AgentDecisionResponse
from app.services.agent_service import AgriAgentService
from app.services.agri_history_service import AgriHistoryService
from app.services.backup_service import BackupService


@pytest_asyncio.fixture
async def history_env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'history.db').as_posix()}")
    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        users = {}
        for index, (name, role) in enumerate([('owner', 'customer'), ('other', 'customer'),
                ('expert', 'expert'), ('expert2', 'expert'), ('admin', 'admin'), ('cs', 'cs')], 1):
            users[name] = User(id=index, username=name, password_hash='fictional', role=role, is_active=True)
        db.add_all(users.values())
        await db.flush()
        farm = Farm(name='Actual farm', owner_id=users['owner'].id)
        db.add(farm)
        await db.flush()
        field = Field(farm_id=farm.id, name='Actual field', area=1)
        db.add(field)
        await db.commit()
        calls = []
        async def decide(db, request, current_user=None):
            calls.append(request.query)
            if request.query == 'fail':
                raise RuntimeError('internal provider detail must not be saved')
            return AgentDecisionResponse(query=request.query, conclusion='缺少现场数据', decision_type='GENERAL',
                confidence=0.2, can_proceed=False, need_expert=request.query != 'manual')
        monkeypatch.setattr(AgriAgentService, 'decide', decide)
        app = FastAPI()
        app.include_router(agent_router)
        app.include_router(history_router)
        caller = [users['owner']]
        async def database(): yield db
        app.dependency_overrides[get_db] = database
        async def current_user():
            return await db.get(User, inspect(caller[0]).identity[0], populate_existing=True)
        app.dependency_overrides[get_current_user] = current_user
        async with AsyncClient(transport=ASGITransport(app, raise_app_exceptions=False), base_url='http://test') as client:
            yield SimpleNamespace(db=db, sessions=sessions, client=client, caller=caller,
                                  users=users, field=field, calls=calls)
    await engine.dispose()


async def ask(env, query='question', **values):
    return await env.client.post('/api/agent/decide', json={'query': query, 'request_id': str(uuid4()), **values})


@pytest.mark.asyncio
async def test_archive_replay_ownership_and_automatic_consultation(history_env):
    env = history_env
    payload = {'query': 'question', 'field_id': env.field.id, 'request_id': str(uuid4())}
    first = await env.client.post('/api/agent/decide', json=payload)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result['conversation_id'] and result['turn_id'] and result['consultation_id']
    replay = await env.client.post('/api/agent/decide', json=payload)
    assert replay.json() == result
    assert env.calls == ['question']
    page = (await env.client.get('/api/agent/conversations')).json()
    assert page['total'] == 1
    detail = (await env.client.get('/api/agent/conversations/' + result['conversation_id'])).json()
    assert detail['items'][0]['query'] == 'question'
    assert detail['items'][0]['consultation']['status'] == 'pending'
    assert await env.db.scalar(select(func.count()).select_from(DiseaseRecord)) == 0
    env.caller[0] = env.users['other']
    assert (await env.client.get('/api/agent/conversations')).json()['total'] == 0
    for path in ['/api/agent/conversations/' + result['conversation_id'], '/api/agri-consultations/' + result['consultation_id']]:
        assert (await env.client.get(path)).status_code == 404
    assert (await env.client.post('/api/agent/decide', json=payload)).status_code == 403
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_resource_denial_has_no_archive_or_model_side_effect(history_env):
    env = history_env
    env.caller[0] = env.users['other']
    assert (await ask(env, field_id=env.field.id)).status_code == 403
    assert not env.calls
    assert await env.db.scalar(select(func.count()).select_from(AgriConversation)) == 0
    assert await env.db.scalar(select(func.count()).select_from(AgriTurn)) == 0


@pytest.mark.asyncio
async def test_claim_resolve_feedback_and_duplicate_submit(history_env):
    env = history_env
    result = (await ask(env)).json()
    ticket_url = '/api/agri-consultations/' + result['consultation_id']
    assert (await env.client.get('/api/agri-consultations')).status_code == 403
    assert (await env.client.post(ticket_url + '/claim')).status_code == 403
    env.caller[0] = env.users['cs']
    assert (await env.client.get('/api/agri-consultations')).status_code == 403
    assert (await env.client.post(ticket_url + '/claim')).status_code == 403
    env.caller[0] = env.users['expert']
    assert (await env.client.post(ticket_url + '/resolve', json={'answer': 'answer'})).status_code == 409
    first = await env.client.post(ticket_url + '/claim')
    assert first.status_code == 200
    assert (await env.client.post(ticket_url + '/claim')).json() == first.json()
    env.caller[0] = env.users['expert2']
    assert (await env.client.post(ticket_url + '/claim')).status_code == 409
    assert (await env.client.post(ticket_url + '/resolve', json={'answer': 'unauthorized overwrite'})).status_code == 409
    env.caller[0] = env.users['expert']
    assert (await env.client.post(ticket_url + '/resolve', json={'answer': '   '})).status_code == 422
    answer = await env.client.post(ticket_url + '/resolve', json={'answer': ' 已核实，先补充资料 '})
    assert answer.status_code == 200
    retry = await env.client.post(ticket_url + '/resolve', json={'answer': '已核实，先补充资料'})
    assert retry.json() == answer.json()
    assert (await env.client.post(ticket_url + '/resolve', json={'answer': 'changed'})).status_code == 409
    env.caller[0] = env.users['owner']
    history = (await env.client.get('/api/agent/conversations/' + result['conversation_id'])).json()
    assert history['items'][0]['consultation']['answer'] == '已核实，先补充资料'
    assert history['items'][0]['consultation']['status'] == 'resolved'


@pytest.mark.asyncio
async def test_manual_transfer_is_idempotent_and_persists_replay(history_env):
    env = history_env
    request_id = str(uuid4())
    result = (await ask(env, 'manual', request_id=request_id)).json()
    assert result['consultation_id'] is None
    url = '/api/agent/turns/' + result['turn_id'] + '/consultation'
    first = (await env.client.post(url)).json()
    second = (await env.client.post(url)).json()
    assert first == second
    replay = (await ask(env, 'manual', request_id=request_id)).json()
    assert replay['consultation_id'] == first['id']
    assert env.calls == ['manual']
    assert await env.db.scalar(select(func.count()).select_from(AgriConsultation)) == 1


@pytest.mark.asyncio
async def test_field_binding_and_changed_retry_are_rejected(history_env):
    env = history_env
    request_id = str(uuid4())
    result = (await ask(env, request_id=request_id, field_id=env.field.id)).json()
    assert (await ask(env, 'changed', request_id=request_id, field_id=env.field.id)).status_code == 409
    assert (await ask(env, conversation_id=result['conversation_id'])).status_code == 409
    followup = await ask(env, 'another question', conversation_id=result['conversation_id'], field_id=env.field.id)
    assert followup.status_code == 200
    assert followup.json()['conversation_id'] == result['conversation_id']
    assert env.calls == ['question', 'another question']
    assert (await ask(env, '   ')).status_code == 422


@pytest.mark.asyncio
async def test_failed_request_is_observable_and_never_replayed(history_env):
    env = history_env
    request_id = str(uuid4())
    assert (await ask(env, 'fail', request_id=request_id)).status_code == 500
    assert (await ask(env, 'fail', request_id=request_id)).status_code == 409
    assert env.calls == ['fail']
    conversation = await env.db.scalar(select(AgriConversation))
    history = (await env.client.get('/api/agent/conversations/' + conversation.id)).json()
    turn = history['items'][0]
    assert turn['status'] == 'failed' and turn['decision'] is None
    assert 'provider' not in turn['error']
    assert (await env.client.post('/api/agent/turns/' + turn['id'] + '/consultation')).status_code == 409


@pytest.mark.asyncio
async def test_business_date_search_and_literal_wildcards(history_env):
    env = history_env
    first = (await ask(env, 'literal % query')).json()
    second = (await ask(env, 'ordinary query')).json()
    await env.db.execute(update(AgriConversation).where(AgriConversation.id == first['conversation_id'])
        .values(updated_at=datetime(2026, 10, 8, 16, 30, tzinfo=timezone.utc)))
    await env.db.execute(update(AgriConversation).where(AgriConversation.id == second['conversation_id'])
        .values(updated_at=datetime(2026, 10, 9, 16, 30, tzinfo=timezone.utc)))
    await env.db.commit()
    date_page = (await env.client.get('/api/agent/conversations', params={'date_from': '2026-10-09', 'date_to': '2026-10-09'})).json()
    assert date_page['total'] == 1 and date_page['items'][0]['id'] == first['conversation_id']
    assert (await env.client.get('/api/agent/conversations', params={'q': '%'})).json()['total'] == 1
    assert (await env.client.get('/api/agent/conversations', params={'date_from': '2026-10-10', 'date_to': '2026-10-09'})).status_code == 422
    assert (await env.client.get('/api/agent/conversations', params={'date_from': '0001-01-01', 'date_to': '9999-12-31'})).status_code == 200


@pytest.mark.asyncio
async def test_user_id_reuse_cannot_inherit_history(history_env):
    env = history_env
    env.caller[0] = env.users['other']  # Owns no farm; pure consultation is independent of a field.
    result = (await ask(env)).json()
    reused_id = env.users['other'].id
    await env.db.execute(delete(User).where(User.id == reused_id))
    await env.db.commit()
    if env.users['other'] in env.db:
        env.db.expunge(env.users['other'])
    replacement = User(id=reused_id, username='replacement', role='customer', password_hash='fictional', is_active=True)
    env.db.add(replacement)
    await env.db.commit()
    env.caller[0] = replacement
    assert (await env.client.get('/api/agent/conversations')).json()['total'] == 0
    assert (await env.client.get('/api/agent/conversations/' + result['conversation_id'])).status_code == 404
    assert (await env.client.get('/api/agri-consultations/' + result['consultation_id'])).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'is_active': False}, {'role': 'customer'}])
async def test_expert_disable_or_demotion_requeues(history_env, change):
    env = history_env
    result = (await ask(env)).json()
    env.caller[0] = env.users['expert']
    url = '/api/agri-consultations/' + result['consultation_id']
    await env.client.post(url + '/claim')
    await update_user(env.users['expert'].id, UpdateUserRequest(**change), env.db, env.users['admin'])
    env.caller[0] = env.users['expert2']
    ticket = (await env.client.get(url)).json()
    assert ticket['status'] == 'pending' and ticket['assigned_expert_id'] is None
    assert (await env.client.post(url + '/claim')).status_code == 200
    assert CreateUserRequest(username='new', password='fictional', display_name='Expert', phone='13800000001', role='expert').role == 'expert'


@pytest.mark.asyncio
async def test_pending_request_guard_and_timeout(history_env):
    env = history_env
    entered, finish = asyncio.Event(), asyncio.Event()
    original = AgriAgentService.decide
    async def slow(db, request, current_user=None):
        entered.set()
        await finish.wait()
        return await original(db, request, current_user)
    AgriAgentService.decide = slow
    request_id = str(uuid4())
    async def run_first():
        async with env.sessions() as db:
            return await AgriHistoryService.decide(db, AgentDecisionRequest(query='slow', request_id=request_id), env.users['owner'])
    task = asyncio.create_task(run_first())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        conversation = await env.db.scalar(select(AgriConversation))
        conversation_id = conversation.id
        assert (await ask(env, 'slow', request_id=request_id)).status_code == 409
        assert (await ask(env, 'other request', conversation_id=conversation_id)).status_code == 409
        await env.db.commit()
        await env.db.execute(update(AgriTurn).values(created_at=datetime.now(timezone.utc) - timedelta(minutes=31)))
        await env.db.commit()
        history = (await env.client.get('/api/agent/conversations/' + conversation_id)).json()
        assert history['items'][0]['status'] == 'failed'
        finish.set()
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(task, 5)
        assert error.value.status_code == 409
        assert await env.db.scalar(select(func.count()).select_from(AgriConsultation)) == 0
    finally:
        finish.set()
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        AgriAgentService.decide = original


@pytest.mark.asyncio
async def test_concurrent_claim_only_one_expert_wins(history_env):
    env = history_env
    result = (await ask(env)).json()
    await env.db.commit()
    async def claim(user):
        async with env.sessions() as db:
            try:
                return await AgriHistoryService.claim(db, result['consultation_id'], user)
            except HTTPException as error:
                return error.status_code
    outcomes = await asyncio.gather(claim(env.users['expert']), claim(env.users['expert2']))
    assert sum(value == 409 for value in outcomes) == 1
    assert sum(getattr(value, 'status', None) == 'in_review' for value in outcomes) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_questions_never_infer_twice_or_leave_orphan_conversations(history_env):
    env = history_env
    request = AgentDecisionRequest(query='concurrent retry', request_id=str(uuid4()))
    async def decide():
        async with env.sessions() as db:
            try:
                return await AgriHistoryService.decide(db, request, env.users['owner'])
            except HTTPException as error:
                assert error.status_code == 409
                return None
    results = await asyncio.gather(decide(), decide())
    assert any(result is not None for result in results)
    assert env.calls == ['concurrent retry']
    assert await env.db.scalar(select(func.count()).select_from(AgriConversation)) == 1
    assert await env.db.scalar(select(func.count()).select_from(AgriTurn)) == 1
    assert await env.db.scalar(select(func.count()).select_from(AgriConsultation)) == 1


@pytest.mark.asyncio
async def test_stale_account_cannot_create_an_archive_for_reused_user_id(history_env):
    env = history_env
    old = env.users['other']
    stale = SimpleNamespace(id=old.id, auth_subject=old.auth_subject, role=old.role, is_active=True)
    await env.db.execute(delete(User).where(User.id == stale.id))
    await env.db.commit()
    env.db.expunge(old)
    env.db.add(User(id=stale.id, username='new lifetime', password_hash='fictional', role='customer', is_active=True))
    await env.db.commit()
    with pytest.raises(HTTPException) as error:
        await AgriHistoryService.decide(env.db, AgentDecisionRequest(query='old lifetime question'), stale)
    assert error.value.status_code == 401
    assert not env.calls
    assert await env.db.scalar(select(func.count()).select_from(AgriConversation)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'is_active': False}, {'role': 'customer'}])
async def test_stale_expert_request_cannot_reassign_ticket_after_account_change(history_env, change):
    env = history_env
    result = (await ask(env)).json()
    expert = env.users['expert']
    stale = SimpleNamespace(id=expert.id, role='expert', is_active=True, auth_subject=expert.auth_subject)
    await update_user(expert.id, UpdateUserRequest(**change), env.db, env.users['admin'])
    with pytest.raises(HTTPException) as error:
        await AgriHistoryService.claim(env.db, result['consultation_id'], stale)
    assert error.value.status_code == 403
    ticket = await env.db.get(AgriConsultation, result['consultation_id'])
    assert ticket.status == 'pending' and ticket.assigned_expert_id is None


@pytest.mark.asyncio
async def test_backup_includes_archives_without_user_passwords(history_env):
    env = history_env
    result = (await ask(env)).json()
    success, _, data = await BackupService.export_to_json(env.db)
    assert success
    assert data['agri_conversations'][0]['id'] == result['conversation_id']
    assert data['agri_turns'][0]['decision']['consultation_id'] == result['consultation_id']
    assert data['agri_consultations'][0]['status'] == 'pending'
    assert 'password_hash' not in data['users'][0]


@pytest.mark.asyncio
async def test_main_application_does_not_publish_retired_ecommerce_routes():
    from app.main import app
    # Router inclusion internals are not a flat list in current FastAPI versions.
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://local') as client:
        response = await client.get('/openapi.json')
        assert response.status_code == 200
        paths = response.json()['paths']
        for path in ('/api/chat/conversations', '/api/human/conversations', '/api/products'):
            assert (await client.get(path)).status_code == 404
    assert not any(path.startswith(('/api/chat', '/api/human', '/api/products')) for path in paths)
    assert 'get' in paths['/api/agent/conversations']
    assert 'post' in paths['/api/agri-consultations/{ticket_id}/resolve']
