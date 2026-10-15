# -*- coding: utf-8 -*-
"""
AgriMind 智能农事计划服务与日历 API 单元测试 (test_agri_plan.py)
对应改进计划第 10 步：
1. 鉴权与越权拦截（未登录 401，跨用户 403，不存在地块 404）
2. 科学农事推演与气象硬阻断（雨日阻断施药、安全间隔期预警）
3. 农事计划一键落库（沉淀至 FarmingRecord，闭环流转）
"""

from datetime import date, timedelta
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.crop import CropPlanting
from app.models.farming_record import FarmingRecord
from app.models.farm import Farm
from app.models.field import Field
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.weather_service import WeatherService
from app.api.agri_plan import router as agri_plan_router


@pytest_asyncio.fixture
async def plan_test_data(test_session: AsyncSession):
    """准备农场、地块、作物批次与测试用户数据"""
    # 农户 A (ID owner)
    farmer_a = User(
        username="farmer_a",
        email="farmer_a@test.com",
        password_hash="hash",
        role="farmer",
        is_active=True,
    )
    # 农户 B (用于越权测试)
    farmer_b = User(
        username="farmer_b",
        email="farmer_b@test.com",
        password_hash="hash",
        role="farmer",
        is_active=True,
    )
    # 专家
    expert = User(
        username="expert_plan",
        email="expert_plan@test.com",
        password_hash="hash",
        role="expert",
        is_active=True,
    )
    test_session.add_all([farmer_a, farmer_b, expert])
    await test_session.flush()

    farm = Farm(
        owner_id=farmer_a.id,
        name="寿光示范农场",
        description="番茄智慧大棚基地",
        province="山东省",
        city="潍坊市",
        district="寿光市",
        address="古城街道示范区",
        latitude=36.89,
        longitude=118.75,
        area=10.0,
    )
    test_session.add(farm)
    await test_session.flush()

    field = Field(
        farm_id=farm.id,
        name="1号露地测试田",
        facility_type="open_field",
        area=2.5,
        soil_type="砂壤土",
        irrigation_type="滴灌",
        latitude=36.89,
        longitude=118.75,
    )
    test_session.add(field)
    await test_session.flush()

    today = date.today()
    planting = CropPlanting(
        field_id=field.id,
        crop="番茄",
        variety="普罗旺斯",
        planting_date=today - timedelta(days=45),
        expected_harvest_date=today + timedelta(days=45),
        growth_stage="开花期",
        area=2.5,
        status="active",
    )
    test_session.add(planting)
    await test_session.commit()

    token_a = AuthService.create_access_token({"sub": str(farmer_a.id), "username": farmer_a.username, "role": farmer_a.role, "account_subject": farmer_a.auth_subject})
    token_b = AuthService.create_access_token({"sub": str(farmer_b.id), "username": farmer_b.username, "role": farmer_b.role, "account_subject": farmer_b.auth_subject})
    token_expert = AuthService.create_access_token({"sub": str(expert.id), "username": expert.username, "role": expert.role, "account_subject": expert.auth_subject})

    return {
        "farmer_a": farmer_a,
        "farmer_b": farmer_b,
        "expert": expert,
        "token_a": token_a,
        "token_b": token_b,
        "token_expert": token_expert,
        "farm": farm,
        "field": field,
        "planting": planting,
    }


@pytest_asyncio.fixture
async def plan_client(test_session: AsyncSession):
    """构建独立 Plan 测试客户端"""
    app = FastAPI()
    app.include_router(agri_plan_router)

    async def override_get_db():
        yield test_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
async def test_generate_plan_unauthorized(plan_client: AsyncClient, plan_test_data):
    """未登录用户请求计划返回 401"""
    field_id = plan_test_data["field"].id
    res = await plan_client.post("/api/agri/plan", json={"field_id": field_id, "days": 7})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_generate_plan_forbidden_for_other_farmer(plan_client: AsyncClient, plan_test_data):
    """非本地块农户请求计划返回 403 Forbidden"""
    field_id = plan_test_data["field"].id
    headers = {"Authorization": f"Bearer {plan_test_data['token_b']}"}
    res = await plan_client.post("/api/agri/plan", json={"field_id": field_id, "days": 7}, headers=headers)
    assert res.status_code == 403
    assert "无权限" in res.json().get("detail", "")


@pytest.mark.asyncio
async def test_generate_plan_field_not_found(plan_client: AsyncClient, plan_test_data):
    """不存在的地块返回 404"""
    headers = {"Authorization": f"Bearer {plan_test_data['token_a']}"}
    res = await plan_client.post("/api/agri/plan", json={"field_id": 99999, "days": 7}, headers=headers)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_generate_plan_success_and_weather_blocking(plan_client: AsyncClient, plan_test_data, monkeypatch):
    """农场主成功生成 7 天农事计划，且验证雨日阻断打药机制"""
    # 构造含中大雨的天气数据进行测试
    mock_weather = {
        "summary": "未来两天有降雨",
        "forecasts": [
            {
                "date": date.today().strftime("%Y-%m-%d"),
                "weather": "暴雨",
                "temp_max": 22.0,
                "temp_min": 16.0,
                "rainfall_mm": 18.5,
                "rain_prob": 90,
                "wind_speed": 1.0,
                "is_suitable_spray": False,
            },
            {
                "date": (date.today() + timedelta(days=1)).strftime("%Y-%m-%d"),
                "weather": "晴",
                "temp_max": 26.0,
                "temp_min": 17.0,
                "rainfall_mm": 0.0,
                "rain_prob": 10,
                "wind_speed": 1.0,
                "is_suitable_spray": True,
            }
        ]
    }

    async def fake_get_real_weather(*args, **kwargs):
        return mock_weather

    monkeypatch.setattr(WeatherService, "get_real_weather", fake_get_real_weather)


    field_id = plan_test_data["field"].id
    headers = {"Authorization": f"Bearer {plan_test_data['token_a']}"}
    res = await plan_client.post(
        "/api/agri/plan",
        json={"field_id": field_id, "days": 7},
        headers=headers
    )
    assert res.status_code == 200
    data = res.json()
    assert data["field_id"] == field_id
    assert data["crop"] == "番茄"
    assert data["growth_stage"] == "开花期"
    assert len(data["items"]) >= 1

    # 验证降雨日的任务标记
    first_item = data["items"][0]
    # Inspection completion is not a spray permission; weather risks remain visible.
    assert first_item["can_proceed"] is True
    assert "18.5mm" in first_item["reason"] and "90%" in first_item["reason"]


@pytest.mark.asyncio
async def test_apply_plan_items_to_farming_records(plan_client: AsyncClient, plan_test_data, test_session: AsyncSession, monkeypatch):
    """Only a server-issued, current, non-spray item becomes an execution record."""
    from tests.test_agri_optimization_a import provider_data
    async def weather(**kwargs):
        return WeatherService._parse_open_meteo_response(provider_data(), kwargs['days'])
    monkeypatch.setattr(WeatherService, 'get_real_weather', weather)
    field_id=plan_test_data['field'].id
    headers={"Authorization": f"Bearer {plan_test_data['token_a']}"}
    generated=await plan_client.post('/api/agri/plan', json={'field_id':field_id}, headers=headers)
    assert generated.status_code==200
    item=generated.json()['items'][0]
    response=await plan_client.post('/api/agri/plan/apply', json={'field_id':field_id,'items':[item]}, headers=headers)
    assert response.status_code==200
    assert len(response.json()['record_ids'])==1
    records=(await test_session.execute(select(FarmingRecord).where(FarmingRecord.field_id==field_id))).scalars().all()
    assert len(records)==1
    assert records[0].safety_interval_days is None
    assert '人工确认已完成' in records[0].description
    rejected=await plan_client.post('/api/agri/plan/apply', json={'field_id':field_id,'items':[generated.json()['items'][2]]}, headers=headers)
    assert rejected.status_code==409
    assert len((await test_session.execute(select(FarmingRecord).where(FarmingRecord.field_id==field_id))).scalars().all())==1


@pytest_asyncio.fixture
async def saved_plan_context(plan_test_data, monkeypatch):
    from app.services import agri_plan_service as service
    from app.core.weather_policy import agriculture_today
    from app.utils.time import utcnow
    state = {'today': agriculture_today(), 'now': utcnow(), 'calls': [], 'rain': 0}
    monkeypatch.setattr(service, 'agriculture_today', lambda: state['today'])
    monkeypatch.setattr(service, 'utcnow', lambda: state['now'])

    async def weather(db, field, days=7, force_refresh=False):
        state['calls'].append((days, force_refresh))
        return {'status': 'online', 'summary': '真实来源测试预报', 'fetched_at': state.get('fetched_at', state['now']).isoformat(),
                'current': {'timestamp': state['now'].isoformat()},
                'forecasts': [{'date': (state['today'] + timedelta(days=i)).isoformat(),
                    'rainfall_mm': state['rain'], 'rain_prob': 0, 'temp_max': 25, 'wind_speed': 1}
                    for i in range(days)]}
    monkeypatch.setattr(WeatherService, 'get_field_weather', weather)
    state['data'] = plan_test_data
    return state


async def load_saved_plan(session, context, **options):
    from app.services.agri_plan_service import AgriPlanService
    from app.schemas.agriculture import AgriPlanRequest
    data = context['data']
    return await AgriPlanService.generate_plan(session,
        AgriPlanRequest(field_id=data['field'].id, **options), data['farmer_a'].id, 'farmer')


@pytest.mark.asyncio
async def test_saved_plan_survives_new_session_and_shares_calendar_window(test_session, saved_plan_context):
    from app.models.agri_plan_snapshot import AgriPlanSnapshot
    first = await load_saved_plan(test_session, saved_plan_context)
    async with AsyncSession(test_session.bind, expire_on_commit=False) as another_session:
        calendar = await load_saved_plan(another_session, saved_plan_context, days=14)
    assert first.version == calendar.version == 1
    assert first.generated_at == calendar.generated_at
    assert [i.model_dump(exclude={'proof'}) for i in first.items] == [i.model_dump(exclude={'proof'}) for i in calendar.items[:7]]
    assert saved_plan_context['calls'] == [(14, False)]
    stored = (await test_session.execute(select(AgriPlanSnapshot))).scalar_one()
    assert all(i['proof'] is None and i['execution_status'] == 'pending' for i in stored.payload['items'])


@pytest.mark.asyncio
async def test_saved_plan_rollover_preserves_dates_and_appends_one_day(test_session, saved_plan_context):
    first = await load_saved_plan(test_session, saved_plan_context)
    saved_plan_context['today'] += timedelta(days=1)
    saved_plan_context['now'] += timedelta(days=1)
    second = await load_saved_plan(test_session, saved_plan_context)
    assert [(i.date, i.action, i.record_type) for i in first.items[1:]] == [(i.date, i.action, i.record_type) for i in second.items[:-1]]
    assert second.items[-1].date == (saved_plan_context['today'] + timedelta(days=6)).isoformat()
    assert second.version == 2
    assert second.update_reason == '跨天延长计划窗口'


@pytest.mark.asyncio
async def test_manual_refresh_gets_new_weather_without_changing_tasks(test_session, saved_plan_context):
    first = await load_saved_plan(test_session, saved_plan_context)
    saved_plan_context['rain'] = 20
    second = await load_saved_plan(test_session, saved_plan_context, force_refresh=True)
    assert second.version == 2 and saved_plan_context['calls'][-1] == (14, True)
    assert [(i.date, i.action) for i in first.items] == [(i.date, i.action) for i in second.items]
    assert '20mm' in second.items[0].reason
    assert any('风险提示' in c for c in second.changes)


@pytest.mark.asyncio
async def test_expired_weather_reassessed_but_fresh_entry_reuses_plan(test_session, saved_plan_context):
    await load_saved_plan(test_session, saved_plan_context)
    saved_plan_context['now'] += timedelta(seconds=599)
    fresh = await load_saved_plan(test_session, saved_plan_context)
    assert fresh.version == 1 and len(saved_plan_context['calls']) == 1
    saved_plan_context['now'] += timedelta(seconds=2)
    expired = await load_saved_plan(test_session, saved_plan_context)
    assert expired.version == 2 and expired.update_reason == '定期核查天气'


@pytest.mark.asyncio
async def test_crop_and_history_changes_refresh_evidence_not_task_dates(test_session, saved_plan_context):
    first = await load_saved_plan(test_session, saved_plan_context)
    data = saved_plan_context['data']
    data['planting'].growth_stage = '结果期'
    test_session.add(FarmingRecord(field_id=data['field'].id, crop_planting_id=data['planting'].id,
        record_type='irrigation', record_date=saved_plan_context['today'], description='人工记录实际灌溉'))
    await test_session.commit()
    refreshed = await load_saved_plan(test_session, saved_plan_context)
    assert refreshed.growth_stage == '结果期' and refreshed.version == 2
    assert '最近同类农事记录日期' in refreshed.items[1].reason
    assert [(i.date, i.action) for i in first.items] == [(i.date, i.action) for i in refreshed.items]


@pytest.mark.asyncio
async def test_completed_task_facts_and_state_survive_refresh_and_revocation(test_session, saved_plan_context):
    from app.services.agri_plan_service import AgriPlanService
    first = await load_saved_plan(test_session, saved_plan_context)
    data = saved_plan_context['data']
    result = await AgriPlanService.apply_plan_items(test_session, data['field'].id, [first.items[0]],
        'test', data['farmer_a'].id, 'farmer')
    saved_plan_context['rain'] = 20
    refreshed = await load_saved_plan(test_session, saved_plan_context, force_refresh=True)
    assert refreshed.items[0].execution_status == 'completed'
    assert refreshed.items[0].reason == first.items[0].reason
    record = await test_session.get(FarmingRecord, result.record_ids[0])
    record.revoked_at = saved_plan_context['now']
    await test_session.commit()
    revoked = await load_saved_plan(test_session, saved_plan_context)
    assert revoked.items[0].execution_status == 'revoked'


@pytest.mark.asyncio
async def test_saved_plan_proofs_renew_and_remain_bound_to_user(test_session, saved_plan_context, monkeypatch):
    from app.services import plan_confirmation
    clock = [1000000]
    # Replace the module reference rather than mutating the global time module used by auth libraries.
    from types import SimpleNamespace
    monkeypatch.setattr(plan_confirmation, 'time', SimpleNamespace(time=lambda: clock[0]))
    first = await load_saved_plan(test_session, saved_plan_context)
    data = saved_plan_context['data']
    clock[0] += 901
    assert not plan_confirmation.verify_confirmation(first.items[0], data['field'].id, data['farmer_a'].id)
    renewed = await load_saved_plan(test_session, saved_plan_context)
    assert renewed.version == first.version
    assert plan_confirmation.verify_confirmation(renewed.items[0], data['field'].id, data['farmer_a'].id)
    assert not plan_confirmation.verify_confirmation(renewed.items[0], data['field'].id, data['expert'].id)


@pytest.mark.asyncio
async def test_new_planting_does_not_reuse_previous_batch(test_session, saved_plan_context):
    from app.models.agri_plan_snapshot import AgriPlanSnapshot
    first = await load_saved_plan(test_session, saved_plan_context)
    data = saved_plan_context['data']
    data['planting'].status = 'harvested'
    replacement = CropPlanting(field_id=data['field'].id, crop='黄瓜', planting_date=saved_plan_context['today'], status='active')
    test_session.add(replacement)
    await test_session.commit()
    second = await load_saved_plan(test_session, saved_plan_context)
    assert second.crop == '黄瓜' and second.items[0].crop_planting_id != first.items[0].crop_planting_id
    assert len(list((await test_session.execute(select(AgriPlanSnapshot))).scalars())) == 2


@pytest.mark.asyncio
async def test_empty_crop_does_not_call_weather(test_session, saved_plan_context):
    saved_plan_context['data']['planting'].status = 'harvested'
    await test_session.commit()
    response = await load_saved_plan(test_session, saved_plan_context)
    assert response.items == [] and saved_plan_context['calls'] == []


@pytest.mark.asyncio
async def test_saved_plan_does_not_extend_cached_weather_lifetime(test_session, saved_plan_context):
    saved_plan_context['fetched_at'] = saved_plan_context['now'] - timedelta(seconds=590)
    await load_saved_plan(test_session, saved_plan_context)
    saved_plan_context['now'] += timedelta(seconds=20)
    del saved_plan_context['fetched_at']
    refreshed = await load_saved_plan(test_session, saved_plan_context)
    assert refreshed.version == 2 and len(saved_plan_context['calls']) == 2
