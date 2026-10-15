"""Registration approval/contact and revoked history through actual API boundaries."""
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy import create_engine, select, text, inspect
from app.api.auth import router as auth_router
from app.api.users import router as users_router
from app.api.farming_records import router as records_router
from app.database import get_db
from app.models.user import User
from app.models.farm import Farm
from app.models.field import Field
from app.models.farming_record import FarmingRecord
from app.services.auth_service import AuthService
from app.migrations.migrate_registration_review import migrate_registration_review


def authorization(user):
    access, _, _ = AuthService.create_tokens(user.id, user.username, user.auth_subject)
    return {'Authorization': f'Bearer {access}'}


@pytest_asyncio.fixture
async def review_api(test_session, monkeypatch):
    from app.api import auth
    from app.middleware.rate_limit import FailureWindowLimiter
    monkeypatch.setattr(auth, '_login_failures', FailureWindowLimiter())
    admin = User(username='review-admin', display_name='审核管理员', password_hash='synthetic', role='admin', is_active=True)
    test_session.add(admin)
    await test_session.commit()
    app = FastAPI()
    app.include_router(auth_router); app.include_router(users_router); app.include_router(records_router)
    async def database():
        yield test_session
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield client, admin


def registration(username='review-expert', role='expert'):
    return {'username': username, 'password': 'test-password', 'display_name': '测试申请人',
            'phone': '+86 138-0000-0001', 'role': role}


@pytest.mark.asyncio
async def test_expert_waits_for_admin_then_can_login(review_api, test_session):
    client, admin = review_api
    submitted = await client.post('/api/auth/register-customer', json=registration())
    assert submitted.status_code == 201
    assert submitted.json()['status'] == 'pending'
    assert 'access_token' not in submitted.json() and 'refresh_token' not in submitted.json()
    expert = await test_session.scalar(select(User).where(User.username == 'review-expert'))
    assert expert.role == 'expert' and not expert.is_active and expert.phone == '+8613800000001'
    login = {'username': expert.username, 'password': 'test-password'}
    assert (await client.post('/api/auth/login', json=login)).status_code == 403
    assert (await client.get('/api/auth/me', headers=authorization(expert))).status_code == 401
    assert (await client.post('/api/users/%s/approve' % expert.id, headers=authorization(expert))).status_code == 401
    bypass = await client.put(f'/api/users/{expert.id}', json={'is_active': True}, headers=authorization(admin))
    assert bypass.status_code == 409
    approved = await client.post(f'/api/users/{expert.id}/approve', headers=authorization(admin))
    assert approved.status_code == 200
    assert approved.json()['registration_status'] == 'approved'
    assert approved.json()['role'] == 'expert' and approved.json()['registration_reviewed_at']
    signed_in = await client.post('/api/auth/login', json=login)
    assert signed_in.status_code == 200 and signed_in.json()['user']['role'] == 'expert'
    # An idempotent approve retry succeeds; a later rejection cannot undo approval.
    assert (await client.post(f'/api/users/{expert.id}/approve', headers=authorization(admin))).status_code == 200
    assert (await client.post(f'/api/users/{expert.id}/reject', headers=authorization(admin))).status_code == 409


@pytest.mark.asyncio
async def test_farmer_auto_approval_and_all_application_history(review_api, test_session):
    client, admin = review_api
    farmer_response = await client.post('/api/auth/register-customer', json=registration('review-farmer', 'customer'))
    assert farmer_response.status_code == 201 and farmer_response.json()['access_token']
    assert farmer_response.json()['user']['registration_status'] == 'approved'
    farmer = await test_session.get(User, farmer_response.json()['user']['id'])
    # Disabled old accounts do not become pending registrations.
    legacy = User(username='legacy-disabled', password_hash='synthetic', role='expert', is_active=False)
    test_session.add(legacy); await test_session.commit()
    await client.post('/api/auth/register-customer', json=registration())
    expert = await test_session.scalar(select(User).where(User.username == 'review-expert'))
    assert (await client.get('/api/users/registrations', headers=authorization(farmer))).status_code == 403
    before = await client.get('/api/users/registrations', headers=authorization(admin))
    assert before.json()['total'] == 2
    pending = await client.get('/api/users/pending', headers=authorization(admin))
    assert [item['id'] for item in pending.json()] == [expert.id]
    assert (await client.post(f'/api/users/{expert.id}/reject', headers=authorization(admin))).status_code == 204
    assert (await client.post(f'/api/users/{expert.id}/reject', headers=authorization(admin))).status_code == 204
    rejected = await client.get('/api/users/registrations?registration_status=rejected', headers=authorization(admin))
    assert rejected.json()['total'] == 1 and rejected.json()['items'][0]['phone'] == '+8613800000001'
    await test_session.refresh(expert)
    assert expert.registration_status == 'rejected' and not expert.is_active
    assert expert.registration_reviewed_by_subject == admin.auth_subject
    assert (await client.post('/api/auth/login', json={'username': expert.username, 'password': 'test-password'})).status_code == 403
    assert (await client.post(f'/api/users/{legacy.id}/approve', headers=authorization(admin))).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize('phone', [None, '', '  ', 'abcdefghi', '123', '1'*30])
async def test_required_phone_is_checked_on_all_creation_routes(review_api, phone):
    client, admin = review_api
    body = registration()
    if phone is None:
        body.pop('phone')
    else:
        body['phone'] = phone
    for url in ('/api/auth/register-customer', '/api/auth/register', '/api/users'):
        response = await client.post(url, json=body, headers=authorization(admin))
        assert response.status_code == 422, url


@pytest.mark.asyncio
async def test_public_registration_cannot_request_admin_role_and_admin_creation_stores_phone(review_api):
    client, admin = review_api
    body = registration(); body['role'] = 'admin'
    assert (await client.post('/api/auth/register-customer', json=body)).status_code == 422
    assert (await client.post('/api/auth/register', json=body)).status_code == 422
    body['role'] = 'expert'
    created = await client.post('/api/users', json=body, headers=authorization(admin))
    assert created.status_code == 201 and created.json()['phone'] == '+8613800000001'
    assert created.json()['registration_status'] is None and created.json()['is_active']


@pytest.mark.asyncio
async def test_revoke_stays_in_auditable_history_but_out_of_active_records(review_api, test_session):
    client, admin = review_api
    owner = User(username='record-owner', password_hash='synthetic', display_name='记录农户', role='customer', is_active=True)
    stranger = User(username='record-stranger', password_hash='synthetic', role='customer', is_active=True)
    test_session.add_all([owner, stranger]); await test_session.flush()
    farm = Farm(name='历史档案农场', owner_id=owner.id)
    test_session.add(farm); await test_session.flush()
    field = Field(name='历史档案田块', farm_id=farm.id)
    test_session.add(field); await test_session.flush()
    from app.core.weather_policy import agriculture_today
    record = FarmingRecord(field_id=field.id, record_date=agriculture_today(), record_type='irrigation',
                           description='原始农事内容', created_by_subject=owner.auth_subject,
                           attachments={'from_agri_plan': True, 'weather_evidence': {'status': 'offline'}})
    test_session.add(record); await test_session.commit()
    headers = authorization(owner)
    revoked = await client.delete(f'/api/farming-records/{record.id}', params={'reason': '日期录错'}, headers=headers)
    assert revoked.status_code == 200
    active = await client.get(f'/api/farming-records/field/{field.id}', headers=headers)
    assert active.json() == []
    for url in (f'/api/farming-records/field/{field.id}', f'/api/fields/{field.id}/farming-records'):
        history = await client.get(url, params={'record_status': 'revoked'}, headers=headers)
        items = history.json() if isinstance(history.json(), list) else history.json()['items']
        assert len(items) == 1 and items[0]['id'] == record.id
        assert items[0]['revoke_reason'] == '日期录错' and items[0]['revoked_at']
        assert items[0]['revoked_by_name'] == '记录农户' and not items[0]['can_delete']
        foreign = await client.get(url, params={'record_status': 'all'}, headers=authorization(stranger))
        assert foreign.status_code == 403
    assert await test_session.get(FarmingRecord, record.id) is not None


def test_additive_registration_migration_is_idempotent_and_preserves_legacy_accounts():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50), is_active BOOLEAN)'))
        connection.execute(text("INSERT INTO users VALUES (1, 'old-user', 0)"))
        migrate_registration_review(connection); migrate_registration_review(connection)
        assert {'phone', 'registration_status', 'registration_reviewed_at'} <= {c['name'] for c in inspect(connection).get_columns('users')}
        old = connection.execute(text('SELECT username, is_active, phone, registration_status FROM users')).one()
        assert tuple(old) == ('old-user', 0, None, None)
    engine.dispose()
