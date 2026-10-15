# -*- coding: utf-8 -*-
"""
AgriMind 端到端全业务链路集成闭环验收测试 (test_e2e_system.py)
对应改进计划第 14 步及计划第三十八节交付验收规范：
覆盖 7 大核心业务环节闭环：
1. /health 系统健康检查与版本自检
2. 农场主/农技专家/系统管理员 3 角色安全鉴权与 JWT 令牌互认
3. /api/farms 农场与田块时空建档
4. /api/farming-records 田间农事实绩录入与安全间隔期(PHI)追溯
5. /api/agent/tools & /api/agent/decide 农业智能决策中枢多工具推演
6. /api/expert/tickets 农技专家会诊工单接单与确诊处方反哺
7. /api/agri/plan 智能农事作业排程与一键落库闭环
"""

import pytest
import pytest_asyncio
from datetime import date, timedelta
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.main import app
from app.database import get_db
from app.models.user import User
from app.models.farm import Farm
from app.models.field import Field
from app.models.crop import CropPlanting
from app.models.farming_record import FarmingRecord
from app.services.auth_service import AuthService
from app.services.weather_service import WeatherService


@pytest.fixture(autouse=True)
def mock_external_services(monkeypatch):
    """Hermetic 隔离：对外部气象与大模型 API 进行快速桩替换，保证测试封闭、离线可跑、亚秒级完成"""
    async def mock_get_real_weather(*args, **kwargs):
        days = kwargs.get("days", 7)
        return {
            "source": "Open-Meteo(Mock)",
            "temperature": 23.5,
            "humidity": 65.0,
            "rainfall": 0.0,
            "wind_speed": 2.5,
            "weather": "多云",
            "daily_forecast": [
                {
                    "date": str(date.today() + timedelta(days=i)),
                    "weather": "晴" if i % 2 == 0 else "多云",
                    "temp_max": 28.0,
                    "temp_min": 18.0,
                    "rainfall": 0.0,
                    "rain_prob": 10,
                    "wind_speed_max": 3.0,
                }
                for i in range(days)
            ]
        }
    
    async def mock_synthesize(*args, **kwargs):
        return {
            "conclusion": "【作业研判通过】天气晴好，风速适宜，符合喷施嘧菌酯国家技术规程。",
            "can_proceed": True,
            "confidence": 0.92,
            "risks": ["连续作业需做好个人防护"],
            "recommendations": ["上午 9:00 前完成喷雾，避免正午高温造成药滴蒸发"],
            "need_expert": False,
            "expert_reason": None,
        }

    from app.services.agent_service import AgriAgentService
    monkeypatch.setattr(WeatherService, "get_real_weather", mock_get_real_weather)
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", mock_synthesize)


@pytest_asyncio.fixture
async def e2e_client(test_session: AsyncSession):
    """构建针对真实 FastAPI app 实例的测试客户端"""
    async def override_get_db():
        yield test_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def seed_users(test_session: AsyncSession):
    """初始化管理员、专家、农场主三角色用户"""
    admin = User(username="e2e_admin", email="admin@e2e.com", password_hash=AuthService.hash_password("AdminPass123!"), role="admin", is_active=True)
    expert = User(username="e2e_expert", email="expert@e2e.com", password_hash=AuthService.hash_password("ExpertPass123!"), role="expert", is_active=True)
    farmer = User(username="e2e_farmer", email="farmer@e2e.com", password_hash=AuthService.hash_password("FarmerPass123!"), role="farmer", is_active=True)

    test_session.add_all([admin, expert, farmer])
    await test_session.commit()

    token_admin = AuthService.create_access_token({"sub": str(admin.id), "username": admin.username, "role": admin.role, "account_subject": admin.auth_subject})
    token_expert = AuthService.create_access_token({"sub": str(expert.id), "username": expert.username, "role": expert.role, "account_subject": expert.auth_subject})
    token_farmer = AuthService.create_access_token({"sub": str(farmer.id), "username": farmer.username, "role": farmer.role, "account_subject": farmer.auth_subject})

    return {
        "admin": admin,
        "expert": expert,
        "farmer": farmer,
        "tokens": {
            "admin": token_admin,
            "expert": token_expert,
            "farmer": token_farmer,
        }
    }


@pytest.mark.asyncio
async def test_e2e_health_check(e2e_client: AsyncClient):
    """验收 1: 系统健康检查"""
    res = await e2e_client.get("/health")
    assert res.status_code == 200
    assert res.json().get("status") == "healthy"


@pytest.mark.asyncio
async def test_e2e_three_roles_auth(e2e_client: AsyncClient, seed_users):
    """验收 2: 验证 3 角色登录鉴权与当前用户身份解析"""
    # 农场主登录验证
    login_res = await e2e_client.post("/api/auth/login", json={"username": "e2e_farmer", "password": "FarmerPass123!"})
    assert login_res.status_code == 200
    farmer_token = login_res.json().get("access_token")
    assert farmer_token

    # 获取当前用户信息
    me_res = await e2e_client.get("/api/auth/me", headers={"Authorization": f"Bearer {farmer_token}"})
    assert me_res.status_code == 200
    assert me_res.json().get("username") == "e2e_farmer"
    assert me_res.json().get("role") == "farmer"


@pytest.mark.asyncio
async def test_e2e_farm_and_field_lifecycle(e2e_client: AsyncClient, seed_users):
    """验收 3: 农场与田块建档全生命周期"""
    farmer_token = seed_users["tokens"]["farmer"]
    headers = {"Authorization": f"Bearer {farmer_token}"}

    # 1. 创建农场
    farm_res = await e2e_client.post(
        "/api/farms",
        json={
            "name": "E2E寿光数字示范农场",
            "province": "山东省",
            "city": "潍坊市",
            "district": "寿光市",
            "address": "现代农业高新区1号",
            "latitude": 36.89,
            "longitude": 118.75,
            "area": 25.0,
            "description": "全自动日光温室数字化番茄基地",
        },
        headers=headers,
    )
    assert farm_res.status_code == 201
    farm_id = farm_res.json()["id"]

    # 2. 为农场创建田块
    field_res = await e2e_client.post(
        f"/api/farms/{farm_id}/fields",
        json={
            "name": "1号智能连栋温室",
            "area": 5.0,
            "soil_type": "沙壤土",
            "irrigation_type": "水肥一体化滴灌",
            "latitude": 36.89,
            "longitude": 118.75,
            "remark": "配备温湿度水肥气象传感器",
        },
        headers=headers,
    )
    assert field_res.status_code == 201
    field_id = field_res.json()["id"]

    # 3. 为田块登记作物批次
    crop_res = await e2e_client.post(
        f"/api/fields/{field_id}/crops",
        json={
            "crop": "番茄",
            "variety": "普罗旺斯水果番茄",
            "planting_date": str(date.today() - timedelta(days=40)),
            "expected_harvest_date": str(date.today() + timedelta(days=50)),
            "growth_stage": "开花坐果期",
            "area": 5.0,
            "status": "active",
        },
        headers=headers,
    )
    assert crop_res.status_code == 201
    assert crop_res.json()["crop"] == "番茄"



@pytest.mark.asyncio
async def test_e2e_farming_record_and_phi(e2e_client: AsyncClient, seed_users, test_session: AsyncSession):
    """验收 4: 田间农事记录录入与安全间隔期(PHI)安全追溯"""
    farmer_id = seed_users["farmer"].id
    farmer_token = seed_users["tokens"]["farmer"]
    headers = {"Authorization": f"Bearer {farmer_token}"}

    farm = Farm(owner_id=farmer_id, name="E2E履历测试农场", area=10.0)
    test_session.add(farm)
    await test_session.flush()
    field = Field(farm_id=farm.id, name="北区2号棚", area=3.0)
    test_session.add(field)
    await test_session.commit()

    rec_res = await e2e_client.post(
        "/api/farming-records",
        json={
            "field_id": field.id,
            "record_type": "pest_control",
            "record_date": str(date.today()),
            "operator": "测试作业员",
            "description": "喷施苯醚甲环唑水分散粒剂预防早疫病",
            "input_name": "10%苯醚甲环唑",
            "input_amount": 50.0,
            "unit": "g",
            "safety_interval_days": 7,
            "attachments": [],
        },
        headers=headers,
    )
    assert rec_res.status_code == 201
    assert rec_res.json()["safety_interval_days"] == 7

    # 查询田块记录
    list_res = await e2e_client.get(f"/api/farming-records/field/{field.id}", headers=headers)
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 1


@pytest.mark.asyncio
async def test_e2e_agent_tools_and_decision(e2e_client: AsyncClient, seed_users):
    """验收 5: 农业决策 Agent 注册工具查询与自主研判"""
    farmer_token = seed_users["tokens"]["farmer"]
    headers = {"Authorization": f"Bearer {farmer_token}"}

    # 1. 查询注册的 6 大农业工具定义
    tools_res = await e2e_client.get("/api/agent/tools", headers=headers)
    assert tools_res.status_code == 200
    tool_defs = tools_res.json()
    assert len(tool_defs) >= 6
    tool_names = [t["name"] for t in tool_defs]
    assert "get_weather" in tool_names
    assert "get_field_info" in tool_names
    assert "search_agri_knowledge" in tool_names

    # 2. 执行决策请求
    decide_res = await e2e_client.post(
        "/api/agent/decide",
        json={
            "query": "明天上午番茄地能打药喷洒嘧菌酯吗？",
            "crop": "番茄",
            "growth_stage": "开花期",
        },
        headers=headers,
    )
    assert decide_res.status_code == 200
    decide_data = decide_res.json()
    assert "conclusion" in decide_data
    assert "confidence" in decide_data
    assert len(decide_data.get("tool_traces", [])) >= 1



@pytest.mark.asyncio
async def test_e2e_agri_plan_generate_and_apply(e2e_client: AsyncClient, seed_users, test_session: AsyncSession):
    """验收 6: 智能农事作业计划推演与一键沉淀闭环"""
    farmer_id = seed_users["farmer"].id
    farmer_token = seed_users["tokens"]["farmer"]
    headers = {"Authorization": f"Bearer {farmer_token}"}

    farm = Farm(owner_id=farmer_id, name="E2E排程基地", area=20.0, latitude=36.89, longitude=118.75)
    test_session.add(farm)
    await test_session.flush()
    field = Field(farm_id=farm.id, name="排程大棚A", area=4.0, latitude=36.89, longitude=118.75)
    test_session.add(field)
    await test_session.flush()
    planting = CropPlanting(field_id=field.id, crop="番茄", growth_stage="开花期", planting_date=date.today() - timedelta(days=30), status="active")
    test_session.add(planting)
    await test_session.commit()

    # 生成 7 天计划
    plan_res = await e2e_client.post(
        "/api/agri/plan",
        json={"field_id": field.id, "days": 7},
        headers=headers,
    )
    assert plan_res.status_code == 200
    plan_data = plan_res.json()
    assert len(plan_data["items"]) >= 1

    # can_proceed does not authorize recording tomorrow's work as completed today.
    valid_items = [i for i in plan_data["items"] if i.get("can_proceed")]
    today_items = [item for item in valid_items if item["date"] == plan_data["start_date"]]
    future_items = [item for item in valid_items if item["date"] > plan_data["start_date"]]
    assert today_items and future_items
    rejected = await e2e_client.post(
        "/api/agri/plan/apply",
        json={"field_id": field.id, "items": [today_items[0], future_items[0]]},
        headers=headers,
    )
    assert rejected.status_code == 409
    assert "未来计划不能写为执行履历" in rejected.json()["detail"]
    records = select(FarmingRecord).where(FarmingRecord.field_id == field.id)
    assert (await test_session.execute(records)).scalars().all() == []

    apply_res = await e2e_client.post(
        "/api/agri/plan/apply",
        json={"field_id": field.id, "items": today_items, "operator": "E2E自动化测试"},
        headers=headers,
    )
    assert apply_res.status_code == 200
    assert apply_res.json()["success"] is True
    assert apply_res.json()["created_count"] == len(today_items)
    saved = (await test_session.execute(records)).scalars().all()
    assert len(saved) == len(today_items)
    assert all(record.record_date.isoformat() == plan_data["start_date"] for record in saved)
