# -*- coding: utf-8 -*-
"""Statistics API tests."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.statistics import router as statistics_router
from app.database import get_db
from app.services.auth_service import AuthService
from tests.legacy_fixtures import LegacyRows
from app.utils.time import utcnow
from fastapi import FastAPI


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def statistics_client(test_session: AsyncSession):
    app = FastAPI()
    app.include_router(statistics_router)

    async def override_get_db():
        yield test_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_token(test_session: AsyncSession) -> str:
    admin = await AuthService.create_user(
        db=test_session,
        username="stats_admin",
        password="stats_admin_pass",
        role="admin",
        display_name="Stats Admin",
        is_active=True,
    )
    return AuthService.create_access_token({"sub": str(admin.id), "username": admin.username, "account_subject": admin.auth_subject})


@pytest.mark.asyncio
async def test_empty_agricultural_statistics_has_no_fabricated_success(statistics_client, admin_token):
    response = await statistics_client.get("/api/statistics/snapshot", headers=_auth_header(admin_token))
    assert response.status_code == 200
    data = response.json()
    assert data["schema_version"] == "agriculture-v1"
    assert data["overview"]["period_turns"] == 0
    assert data["overview"]["completion_rate"] is None
    assert data["overview"]["avg_response_time_ms"] is None
    assert len(data["daily"]) == 14
    assert "total_products" not in data["overview"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method", [("/overview", "get"), ("/daily", "get"),
    ("/categories", "get"), ("/top-questions", "get"), ("/data/delete", "post")])
async def test_retired_statistics_returns_gone_and_preserves_business_data(statistics_client, test_session, admin_token, path, method):
    conv = await LegacyRows.create_conversation(test_session, title="legacy", customer_id="user_1")
    response = await getattr(statistics_client, method)("/api/statistics" + path, headers=_auth_header(admin_token))
    assert response.status_code == 410
    assert await LegacyRows.get_conversation(test_session, conv.id) is not None
    from sqlalchemy import select, func
    from app.models.conversation import Conversation
    assert await test_session.scalar(select(func.count(Conversation.id))) == 1


@pytest.mark.asyncio
async def test_statistics_rejects_unregistered_and_nonadmin_accounts(statistics_client, test_session):
    response = await statistics_client.get("/api/statistics/snapshot")
    assert response.status_code in (401, 403)
    user = await AuthService.create_user(test_session, username="stats_farmer", password="fictional", display_name="Actual farmer", role="customer", is_active=True)
    credential = AuthService.create_access_token({"sub": str(user.id), "username": user.username, "account_subject": user.auth_subject})
    for method, path in [("get", "/snapshot"), ("get", "/export"), ("post", "/data/delete")]:
        assert (await getattr(statistics_client, method)("/api/statistics" + path, headers=_auth_header(credential))).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"start_date": "2026-10-09"}, {"end_date": "2026-10-09"},
    {"start_date": "2026-10-10", "end_date": "2026-10-09"},
    {"start_date": "2026-01-01", "end_date": "2026-10-09"}, {"days": 0}, {"top_limit": 101}])
async def test_statistics_bounds_date_and_query_inputs(statistics_client, admin_token, params):
    assert (await statistics_client.get("/api/statistics/snapshot", params=params, headers=_auth_header(admin_token))).status_code == 422


@pytest.mark.asyncio
async def test_statistics_export_uses_selected_range(statistics_client, admin_token):
    response = await statistics_client.get("/api/statistics/export", headers=_auth_header(admin_token),
        params={"start_date": "2026-10-01", "end_date": "2026-10-09"})
    assert response.status_code == 200
    content = response.json()["content"]
    assert "2026-10-01 至 2026-10-09" in content
    assert "无已结束提问" in content
    assert "商品" not in content
