"""Business boundaries for issue-summary 2; synthetic storage and provider data."""
from datetime import date, timedelta
import importlib.util
from pathlib import Path
import pytest
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text, create_engine
from app.models.user import User
from app.models.farm import Farm
from app.models.field import Field
from app.models.farming_record import FarmingRecord
from app.models.settings import SystemSettings
from app.services.farming_record_service import FarmingRecordService
from app.schemas.agriculture import FarmCreate, FieldCreate, FarmUpdate, FarmingRecordCreate
from app.utils.time import utcnow


async def context(db):
    owner = User(username='owner-issue2', password_hash='synthetic', role='customer', is_active=True)
    admin = User(username='admin-issue2', password_hash='synthetic', role='admin', is_active=True)
    db.add_all([owner, admin]); await db.flush()
    farm = Farm(name='test', owner_id=owner.id, latitude=0, longitude=0)
    db.add(farm); await db.flush()
    field = Field(name='test', farm_id=farm.id, area=1)
    db.add(field); await db.commit()
    return owner, admin, farm, field


@pytest.mark.asyncio
@pytest.mark.parametrize('age,allowed', [(599, True), (600, False), (601, False), (-1, False)])
async def test_revocation_window_server_time(test_session, monkeypatch, age, allowed):
    owner, _, _, field = await context(test_session)
    now = utcnow()
    record = FarmingRecord(field_id=field.id, record_type='irrigation', record_date=date.today(),
        created_at=now-timedelta(seconds=age), created_by_subject=owner.auth_subject)
    test_session.add(record); await test_session.commit(); rid = record.id
    monkeypatch.setattr('app.services.farming_record_service.utcnow', lambda: now)
    if allowed:
        assert await FarmingRecordService.delete_farming_record(test_session, rid, owner.id)
        saved = await test_session.get(FarmingRecord, rid, populate_existing=True)
        assert saved.revoked_at == now and saved.revoked_by_subject == owner.auth_subject
        assert (await FarmingRecordService.list_farming_records(test_session, field.id))[1] == 0
        assert await FarmingRecordService.delete_farming_record(test_session, rid, owner.id)
    else:
        with pytest.raises(HTTPException) as error:
            await FarmingRecordService.delete_farming_record(test_session, rid, owner.id)
        assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_unknown_creator_requires_admin_and_preserves_record(test_session):
    owner, admin, _, field = await context(test_session)
    record = FarmingRecord(field_id=field.id, record_type='irrigation', record_date=date.today(), created_at=utcnow()-timedelta(days=1))
    test_session.add(record); await test_session.commit(); rid = record.id
    with pytest.raises(HTTPException): await FarmingRecordService.delete_farming_record(test_session, rid, owner.id)
    await FarmingRecordService.delete_farming_record(test_session, rid, admin.id, True, '核查误录')
    saved = await test_session.get(FarmingRecord, rid, populate_existing=True)
    assert saved.revoke_reason == '核查误录' and saved.created_by_subject is None


@pytest.mark.asyncio
async def test_creation_records_stable_creator(test_session):
    owner, _, _, field = await context(test_session)
    record = await FarmingRecordService.create_farming_record(test_session, field.id,
        FarmingRecordCreate(record_type='irrigation', record_date=date.today()), owner.id)
    assert record.created_by_subject == owner.auth_subject


@pytest.mark.parametrize('model,data', [
    (FarmCreate, {'name': 'farm', 'latitude': 1}),
    (FarmUpdate, {'latitude': True, 'longitude': 1}),
    (FieldCreate, {'name': 'field', 'latitude': float('nan'), 'longitude': 1}),
    (FarmUpdate, {'latitude': 91, 'longitude': 1}),
    (FarmUpdate, {'latitude': [], 'longitude': 1}),
])
def test_invalid_coordinate_pairs_rejected(model, data):
    with pytest.raises(ValueError): model.model_validate(data)


def test_zero_coordinates_and_region_tuple():
    from app.services.location_service import validate_region, regions
    assert FarmCreate(name='zero', latitude=0, longitude=0).latitude == 0
    p=regions()[0]; c=p['children'][0]; d=c['children'][0]
    validate_region(d['code'], p['name'], c['name'], d['name'])
    with pytest.raises(HTTPException): validate_region(d['code'], '错误省份', c['name'], d['name'])


def test_additive_upgrade_is_idempotent(tmp_path):
    from app.migrations.migrate_issue2 import migrate_issue2
    engine = create_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
    with engine.begin() as db:
        db.execute(text('CREATE TABLE farming_records (id INTEGER PRIMARY KEY, created_at DATETIME)'))
        db.execute(text("INSERT INTO farming_records VALUES (1, '2020-01-01')"))
        db.execute(text('CREATE TABLE farms (id INTEGER PRIMARY KEY)'))
        migrate_issue2(db); migrate_issue2(db)
        row = db.execute(text('SELECT id, created_at, created_by_subject, revoked_at FROM farming_records')).one()
        assert tuple(row) == (1, '2020-01-01', None, None)
    engine.dispose()


@pytest.mark.asyncio
async def test_missing_cipher_is_actionable_http503_and_no_partial_save(test_session, monkeypatch, controlled_dns):
    controlled_dns({'api.siliconflow.cn': ['93.184.216.34']})
    from app.api.settings import router
    from app.database import get_db
    from app.api.dependencies import get_current_user
    from app.config import settings
    owner, admin, _, _ = await context(test_session)
    app = FastAPI(); app.include_router(router)
    async def db(): yield test_session
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: admin
    monkeypatch.setattr(settings, 'SETTINGS_ENCRYPTION_KEY', '')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://checks') as client:
        status = await client.get('/api/settings/security-status')
        assert status.json()['encryption_ready'] is False
        result = await client.put('/api/settings', json={'agri_ai': {'llm_api_key': 'synthetic-input'}})
        assert result.status_code == 503 and '本次未保存' in result.json()['detail']
        assert result.headers['x-error-code'] == 'SETTINGS_ENCRYPTION_UNAVAILABLE'
        assert (await test_session.scalars(select(SystemSettings))).all() == []
        await test_session.refresh(owner)
        app.dependency_overrides[get_current_user] = lambda: owner
        assert (await client.get('/api/settings/security-status')).status_code == 403


def test_launcher_never_overwrites_or_replaces_missing_v3_key(tmp_path):
    path = Path(__file__).resolve().parents[2] / 'backend/run.py'
    spec=importlib.util.spec_from_file_location('local_launcher', path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    target=tmp_path/'cipher.key'
    with pytest.raises(RuntimeError): module.prepare_key('', target, True, True)
    assert not target.exists()
    target.write_text('synthetic-stable-key-' + 'x'*32, encoding='ascii')
    assert module.prepare_key('', target, True) == target.read_text()


@pytest.mark.asyncio
async def test_foreign_farm_and_creator_cannot_revoke(test_session):
    owner, admin, farm, field = await context(test_session)
    other = User(username='other-issue2', password_hash='synthetic', role='customer', is_active=True)
    test_session.add(other); await test_session.commit()
    record = FarmingRecord(field_id=field.id, record_type='irrigation', record_date=date.today(),
        created_by_subject=owner.auth_subject)
    test_session.add(record); await test_session.commit()
    with pytest.raises(HTTPException):
        await FarmingRecordService.delete_farming_record(test_session, record.id, other.id)
    record.created_by_subject = admin.auth_subject; await test_session.commit()
    with pytest.raises(HTTPException):
        await FarmingRecordService.delete_farming_record(test_session, record.id, owner.id)


@pytest.mark.asyncio
async def test_coordinate_edit_and_farm_weather_authorization(test_session, monkeypatch):
    from app.api.farms import router
    from app.database import get_db
    from app.api.dependencies import get_current_user
    from app.services.weather_service import WeatherService
    from app.services.farm_service import FarmService
    owner, admin, farm, field = await context(test_session)
    farm.location_source = 'region_reference'; farm.location_label = '测试区域'
    await test_session.commit()
    calls = []
    async def weather(**kwargs): calls.append(kwargs); return {'summary': '已更新', 'status': 'online'}
    monkeypatch.setattr(WeatherService, 'get_real_weather', weather)
    app=FastAPI(); app.include_router(router)
    async def db(): yield test_session
    app.dependency_overrides[get_db]=db
    caller=[owner]
    app.dependency_overrides[get_current_user]=lambda: caller[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://checks') as client:
        result=await client.get(f'/api/farms/{farm.id}/weather')
        assert result.status_code == 200 and result.json()['coordinate_source'] == 'farm'
        assert '区域参考天气' in result.json()['summary']
        assert calls[0]['latitude'] == 0
        other=User(username='foreign-issue2', password_hash='synthetic', role='customer', is_active=True)
        test_session.add(other); await test_session.commit(); caller[0]=other
        assert (await client.get(f'/api/farms/{farm.id}/weather')).status_code == 403
        assert len(calls)==1
    await FarmService.update_farm(test_session, farm.id, FarmUpdate(address='新地点'), owner.id)
    assert farm.latitude is None and farm.longitude is None


@pytest.mark.asyncio
async def test_statistics_and_decision_history_exclude_revoked_records(test_session):
    from app.services.agent_service import AgriTools
    from app.services.agri_statistics_service import AgriStatisticsService
    owner, _, _, field=await context(test_session)
    record=await FarmingRecordService.create_farming_record(test_session, field.id,
        FarmingRecordCreate(record_type='spraying', record_date=date.today(), input_name='synthetic-input'), owner.id)
    await FarmingRecordService.delete_farming_record(test_session, record.id, owner.id)
    history=await AgriTools.get_farming_history(test_session, field.id)
    assert history['records']==[] and history['last_spray'] is None
    snapshot=await AgriStatisticsService.snapshot(test_session)
    assert snapshot.categories.farming_records == {}
    assert all(day.farming_records == 0 for day in snapshot.daily)


@pytest.mark.asyncio
async def test_revoked_plan_receipt_cannot_recreate_record(test_session, monkeypatch):
    from app.models.crop import CropPlanting
    from app.services.agri_plan_service import AgriPlanService
    from app.schemas.agriculture import AgriPlanRequest
    from app.services.weather_service import WeatherService
    from tests.test_agri_optimization_a import provider_data
    owner, _, _, field=await context(test_session)
    test_session.add(CropPlanting(field_id=field.id, crop='测试作物', planting_date=date.today(), status='active'))
    await test_session.commit()
    async def weather(**kwargs): return WeatherService._parse_open_meteo_response(provider_data(), kwargs['days'])
    monkeypatch.setattr(WeatherService, 'get_real_weather', weather)
    plan=await AgriPlanService.generate_plan(test_session, AgriPlanRequest(field_id=field.id), owner.id, owner.role)
    result=await AgriPlanService.apply_plan_items(test_session, field.id, [plan.items[0]], 'test', owner.id, owner.role)
    rid=result.record_ids[0]
    record=await test_session.get(FarmingRecord, rid)
    assert record.created_by_subject == owner.auth_subject
    await FarmingRecordService.delete_farming_record(test_session, rid, owner.id)
    with pytest.raises(HTTPException) as error:
        await AgriPlanService.apply_plan_items(test_session, field.id, [plan.items[0]], 'test', owner.id, owner.role)
    assert error.value.status_code == 409
    assert len((await test_session.scalars(select(FarmingRecord))).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['valid', 'oversize', 'redirect', 'malformed'])
async def test_place_lookup_uses_fixed_host_bounded_output_and_filters_country(monkeypatch, mode):
    import httpx
    from app.services import location_service as module
    calls=[]
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            if mode=='oversize': yield b'x'*131073
            elif mode=='malformed': yield b'not-json'
            else:
                yield b'{"results":[{"country_code":"CN","name":"place","latitude":0,"longitude":0},{"country_code":"US","latitude":1,"longitude":1}]}'
    def respond(request):
        calls.append(request)
        assert request.url.host == 'geocoding-api.open-meteo.com'
        assert request.url.params['name']=='测试县'
        return httpx.Response(302 if mode=='redirect' else 200, stream=Chunks())
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    if mode=='valid':
        result=await module.search_places('测试县')
        assert len(result['items'])==1 and result['items'][0]['latitude']==0
    else:
        with pytest.raises(HTTPException) as error: await module.search_places('测试县')
        assert error.value.status_code==503
    assert len(calls)==1


@pytest.mark.asyncio
async def test_selected_crop_reaches_real_field_tool(test_session):
    from app.models.crop import CropPlanting
    from app.services.agent_service import AgriToolRegistry
    _, _, _, field=await context(test_session)
    crops=[CropPlanting(field_id=field.id, crop=name, planting_date=date.today(), status='active') for name in ('番茄', '黄瓜')]
    test_session.add_all(crops); await test_session.commit()
    result=await AgriToolRegistry.execute_tool('get_field_info', {'field_id': field.id, 'crop_planting_id': crops[1].id}, test_session)
    assert result.status=='success' and result.result['current_crop']['crop']=='黄瓜'
    other=await AgriToolRegistry.execute_tool('get_field_info', {'field_id': field.id, 'crop_planting_id': 99999}, test_session)
    assert other.result['found'] is False


def test_weak_key_cannot_write_but_historical_cipher_still_reads(monkeypatch):
    from cryptography.fernet import Fernet
    from app.services import settings_service as svc
    from app.config import settings
    monkeypatch.setattr(settings, 'SETTINGS_ENCRYPTION_KEY', 'synthetic-short')
    old='v3:'+Fernet(svc._get_encryption_key()).encrypt(b'historical-synthetic').decode()
    assert svc._decrypt_value(old)=='historical-synthetic'
    with pytest.raises(svc.SettingsEncryptionError): svc._encrypt_value('new-synthetic')
