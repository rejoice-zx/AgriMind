# -*- coding: utf-8 -*-
"""
阶段一安全加固与漏洞封堵专项验证套件 (test_phase1_security_fixes.py)
覆盖验证：
1. VisionService 图片路径严格沙箱白名单（拦截跨目录穿越、绝对敏感文件读取、非图片文件读取）
2. AgriAgent API 访问强制认证与权限收拢（匿名 401，非管理/专家单步执行工具 403）
3. 农业资产与田块读取侧系统性水平越权 (IDOR) 防御（跨用户查询农场、地块、农事记录、病害记录、气象快照均严格 403）
"""

import pytest
import os
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.farm import Farm
from app.models.field import Field
from app.services.vision_service import VisionService
from app.services.auth_service import AuthService
from app.api.farms import router as farms_router
from app.api.farming_records import router as farming_records_router
from app.api.agri_agent import router as agri_agent_router
from app.config import settings


@pytest.mark.asyncio
async def test_vision_service_sandbox_path_traversal():
    """测试视觉服务：跨目录穿越与任意文件读取必须被严格拦截"""
    # 1. 尝试目录穿越至 .env
    res1 = await VisionService.diagnose_leaf_image(
        image_path="/static/uploads/../../.env",
        crop_name="番茄",
    )
    assert res1["suspected_disease"] == "未能获取合法图片文件"
    assert res1["confidence"] is None
    assert res1["need_expert"] is True

    # 2. 尝试传入项目根目录下的绝对文件路径
    env_path = os.path.join(settings.BASE_DIR, ".env")
    res2 = await VisionService.diagnose_leaf_image(
        image_path=env_path,
        crop_name="番茄",
    )
    assert res2["suspected_disease"] == "未能获取合法图片文件"
    assert res2["confidence"] is None
    assert res2["need_expert"] is True

    # 3. 尝试伪造非图片后缀
    res3 = await VisionService.diagnose_leaf_image(
        image_path="/static/uploads/canary.txt",
        crop_name="番茄",
    )
    assert res3["suspected_disease"] == "未能获取合法图片文件"
    assert res3["confidence"] is None
    assert res3["need_expert"] is True


@pytest.mark.asyncio
async def test_agri_agent_unauthenticated_and_rbac(test_session: AsyncSession):
    """测试 Agent 端点：未登录 401，普通农户调试工具 403"""
    farmer = User(id=101, username="farmer_bob", role="customer", is_active=True, password_hash="hash")
    test_session.add(farmer)
    await test_session.commit()

    app = FastAPI()
    app.include_router(agri_agent_router)

    async def override_db():
        yield test_session

    app.dependency_overrides[get_db] = override_db
    transport = ASGITransport(app=app)

    # 1. 匿名调用 /decide 必须返回 401
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/agent/decide", json={"query": "测试提问"})
        assert resp.status_code == 401

    # 2. 匿名调用 /tools/execute 必须返回 401
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/agent/tools/execute", json={"tool_name": "calculate_gdd", "arguments": {}})
        assert resp.status_code == 401

    # 3. 普通农户（customer）调用 /tools/execute 必须返回 403（仅专家/管理员可单步调试内部底层工具）
    farmer_token = AuthService.create_access_token(
        data={"sub": "101", "username": "farmer_bob", "role": "customer", "account_subject": farmer.auth_subject}
    )
    headers = {"Authorization": f"Bearer {farmer_token}"}
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/agent/tools/execute",
            json={"tool_name": "calculate_gdd", "arguments": {}},
            headers=headers,
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_agricultural_assets_idor_protection(test_session: AsyncSession):
    """测试农业资产读取侧 IDOR 水平越权拦截：用户 B 试图查看用户 A 的农场、田块与农事数据必须 403"""
    # 准备用户 A 和 用户 B
    user_a = User(username="farmer_alice", email="alice@test.com", password_hash="hash_a", role="customer", is_active=True)
    user_b = User(username="farmer_bob", email="bob@test.com", password_hash="hash_b", role="customer", is_active=True)
    test_session.add(user_a)
    test_session.add(user_b)
    await test_session.commit()
    await test_session.refresh(user_a)
    await test_session.refresh(user_b)

    # 用户 A 创建其专属农场与田块
    farm_a = Farm(
        owner_id=user_a.id,
        name="爱丽丝专属智慧生态园",
        province="山东省",
        city="寿光市",
        district="文家街道",
        area=50.0,
    )
    test_session.add(farm_a)
    await test_session.commit()
    await test_session.refresh(farm_a)

    field_a = Field(
        farm_id=farm_a.id,
        name="爱丽丝1号番茄大棚",
        area=10.0,
        soil_type="壤土",
        irrigation_type="滴灌",
    )
    test_session.add(field_a)
    await test_session.commit()
    await test_session.refresh(field_a)

    # 构建带 DB 覆盖的客户端
    app = FastAPI()
    app.include_router(farms_router)
    app.include_router(farming_records_router)

    async def override_db():
        yield test_session

    app.dependency_overrides[get_db] = override_db

    token_b = AuthService.create_access_token(
        data={"sub": str(user_b.id), "username": user_b.username, "role": user_b.role, "account_subject": user_b.auth_subject}
    )
    headers_b = {"Authorization": f"Bearer {token_b}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. 用户 B 尝试读取用户 A 的农场详情 -> 403
        r1 = await client.get(f"/api/farms/{farm_a.id}", headers=headers_b)
        assert r1.status_code == 403

        # 2. 用户 B 尝试读取用户 A 的地块详情 -> 403
        r2 = await client.get(f"/api/fields/{field_a.id}", headers=headers_b)
        assert r2.status_code == 403

        # 3. 用户 B 尝试读取用户 A 的农事记录 -> 403
        r3 = await client.get(f"/api/farming-records/field/{field_a.id}", headers=headers_b)
        assert r3.status_code == 403

        # 4. 用户 B 尝试读取用户 A 的微气象最新数据 -> 403
        r4 = await client.get(f"/api/fields/{field_a.id}/weather/latest", headers=headers_b)
        assert r4.status_code == 403

        # 5. 用户 B 尝试读取用户 A 的病虫害记录 -> 403
        r5 = await client.get(f"/api/fields/{field_a.id}/disease-records", headers=headers_b)
        assert r5.status_code == 403
