# -*- coding: utf-8 -*-
"""
阶段三自动化测试：接口规范化、前后端数据对齐与安全防御
- 访客会话多租户物理隔离（防匿名串话）
- SSRF 盲请求安全拦截
- 农事记录标准 Pydantic 模型校验与兼容
- 专家工单 limit/offset 参数兼容与分页
"""

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.agri_history import router as history_router
from app.api.farming_records import router as farming_router
from app.api.expert_workbench import router as expert_router
from app.database import get_db
from app.services.auth_service import AuthService
from tests.legacy_fixtures import LegacyRows
from app.services.settings_service import SettingsService
from app.utils.security import validate_safe_external_url


@pytest_asyncio.fixture
async def api_client(test_session: AsyncSession):
    app = FastAPI()
    app.include_router(history_router)
    app.include_router(farming_router)
    app.include_router(expert_router)

    async def override_get_db():
        yield test_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unregistered_history_access_cannot_use_self_reported_session(api_client, test_session):
    await LegacyRows.create_conversation(test_session, title='Historical private conversation', customer_id='guest_session_abc_123')
    for headers in ({}, {'X-Session-ID': 'session_abc_123'}, {'X-Session-ID': 'other_session_xyz'}):
        response = await api_client.get('/api/agent/conversations', headers=headers)
        assert response.status_code in (401, 403)
        assert 'Historical private conversation' not in response.text


def test_ssrf_validation_blocks_internal_and_cloud_metadata(controlled_dns):
    """验证 SSRF 防护函数严密拦截内网私有地址、元数据与回环"""
    controlled_dns({"api.siliconflow.cn": ["93.184.216.34"]})
    blocked_targets = [
        "http://127.0.0.1:8000/v1",
        "http://localhost:3000",
        "http://0.0.0.0:80",
        "http://169.254.169.254/latest/meta-data",
        "http://10.10.1.1:8080/api",
        "http://192.168.1.1/admin",
        "http://172.16.0.1:9000",
        "http://metadata.google.internal/computeMetadata/v1/",
        "ftp://127.0.0.1/test",
    ]
    for url in blocked_targets:
        is_safe, msg = validate_safe_external_url(url)
        assert not is_safe, f"应拦截内网/非法目标 URL: {url}, 实际返回: {is_safe}, 原因: {msg}"

    # 公网合法目标可通过
    is_safe_sf, _ = validate_safe_external_url("https://api.siliconflow.cn/v1")
    assert is_safe_sf is True


@pytest.mark.asyncio
async def test_settings_service_ssrf_blocking(monkeypatch):
    """验证 SettingsService 在测试连接时对内网地址进行直接拦截"""
    import httpx
    calls = []
    async def forbidden_transport(*args, **kwargs):
        calls.append(True)
        raise AssertionError('Unsafe address must be rejected before transport')
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden_transport)
    success, msg = await SettingsService.test_llm_connection(
        provider="deepseek",
        api_key="sk-dummy",
        api_url="http://127.0.0.1:8000/v1"
    )
    assert success is False
    assert isinstance(msg, str) and msg
    assert calls == []


@pytest.mark.asyncio
async def test_farming_records_pydantic_schema_validation(
    api_client: AsyncClient,
    test_session: AsyncSession,
):
    """验证 /api/farming-records 端点通过 Pydantic 强类型请求模型正确接收并校验"""
    # 建立测试农户与田块
    from app.services.farm_service import FarmService
    from app.schemas.agriculture import FarmCreate, FieldCreate

    farmer = await AuthService.create_user(
        db=test_session,
        username="p3_farmer",
        password="farmer_password",
        role="customer",
        display_name="P3 Farmer",
        is_active=True
    )
    await test_session.commit()
    token = AuthService.create_access_token({"sub": str(farmer.id), "username": farmer.username, "account_subject": farmer.auth_subject})
    headers = {"Authorization": f"Bearer {token}"}

    farm = await FarmService.create_farm(test_session, FarmCreate(name="P3测试农场"), owner_id=farmer.id)
    field = await FarmService.create_field(test_session, farm.id, FieldCreate(name="P3测试田块", area=5.0), user_id=farmer.id)

    # 1. 结构化创建记录
    payload = {
        "field_id": field.id,
        "record_type": "spraying",
        "operation_date": "2026-10-04",
        "chemical_name": "精甲霜·锰锌",
        "dosage": "100ml",
        "description": "喷施预防晚疫病"
    }
    resp = await api_client.post("/api/farming-records", json=payload, headers=headers)
    assert resp.status_code == 201, f"Failed: {resp.status_code} {resp.text}"
    data = resp.json()
    assert data["field_id"] == field.id
    assert data["record_type"] == "spraying"
    assert data["chemical_name"] == "精甲霜·锰锌"
    assert data["input_amount"] == 100.0
    assert data["unit"] == "ml"
    # A chemical name alone cannot establish a product label's safety interval.
    assert data["safety_interval_days"] is None

    # 2. 字段类型错误时触发 Pydantic 422 自动校验拦截
    bad_payload = {
        "field_id": "not_an_int",
        "record_type": "spraying"
    }
    bad_resp = await api_client.post("/api/farming-records", json=bad_payload, headers=headers)
    assert bad_resp.status_code == 422


@pytest.mark.asyncio
async def test_expert_tickets_limit_offset_compatibility(
    api_client: AsyncClient,
    test_session: AsyncSession,
):
    """验证专家工单查询接口支持 limit 与 offset 参数兼容映射"""
    expert = await AuthService.create_user(
        db=test_session,
        username="p3_expert",
        password="expert_password",
        role="expert",
        display_name="P3 Expert",
        is_active=True
    )
    await test_session.commit()
    token = AuthService.create_access_token({"sub": str(expert.id), "username": expert.username, "account_subject": expert.auth_subject})
    headers = {"Authorization": f"Bearer {token}"}

    # 兼容旧参数 limit=10, offset=0
    resp = await api_client.get("/api/expert/tickets?limit=10&offset=0", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
