"""Deletion queue recovery and failure-budget bypass regressions."""
import asyncio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from app.api.users import router as users_router
from app.api import auth as auth_module
from app.database import get_db
from app.middleware.rate_limit import FailureWindowLimiter
from app.models.conversation import Conversation
from app.models.user import User
from app.services.auth_service import AuthService
from tests.active_identity_fixtures import identity_env, access


@pytest.mark.asyncio
async def test_deleted_handling_agent_returns_case_to_queue(identity_env):
    env = identity_env
    async with env.sessions() as db:
        await db.execute(update(Conversation).values(title="Fixture conversation"))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://local") as client:
        result = await client.delete(f"/api/users/{env.ids['staff']}",
            headers={"Authorization": "Bearer " + access(env, "admin")})
        assert result.status_code == 204
        async with env.sessions() as db:
            conv = await db.get(Conversation, "handled-own")
            assert (conv.status, conv.human_agent_id) == ("pending_human", None)
        assert not any(path.startswith('/api/human') for path in env.app.openapi()['paths'])
        assert (await client.get('/api/human/conversations',
            headers={'Authorization': 'Bearer ' + access(env, 'admin')})).status_code == 404


@pytest.mark.asyncio
async def test_user_delete_does_not_load_retired_notification_service(identity_env, monkeypatch):
    import builtins
    env = identity_env
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == 'app.services.ws_manager':
            raise AssertionError('Retired transport must not be loaded during user deletion')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url='http://local') as client:
        result = await client.delete(f"/api/users/{env.ids['staff']}", headers={'Authorization': 'Bearer ' + access(env, 'admin')})
        assert result.status_code == 204
    async with env.sessions() as db:
        assert await db.get(User, env.ids['staff']) is None
        conv = await db.get(Conversation, 'handled-own')
        assert (conv.status, conv.human_agent_id) == ('pending_human', None)


@pytest.mark.asyncio
async def test_known_account_success_cannot_reset_other_failures(identity_env, monkeypatch):
    env = identity_env
    monkeypatch.setattr(auth_module, "_login_failures", FailureWindowLimiter(max_failures=5))
    async with env.sessions() as db:
        db.add(User(username="known_account", password_hash=AuthService.hash_password("fixture-password"),
                    role="customer", is_active=True))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://local") as client:
        for _ in range(4):
            assert (await client.post("/api/auth/login", json={"username": "victim", "password": "wrong"})).status_code == 401
        assert (await client.post("/api/auth/login", json={"username": "known_account", "password": "fixture-password"})).status_code == 200
        assert (await client.post("/api/auth/login", json={"username": "victim", "password": "wrong"})).status_code == 401
        assert (await client.post("/api/auth/login", json={"username": "known_account", "password": "fixture-password"})).status_code == 429


@pytest.mark.asyncio
async def test_concurrent_authentication_reserves_failure_budget(monkeypatch):
    app = FastAPI()
    app.include_router(auth_module.router)
    app.dependency_overrides[get_db] = lambda: None
    limiter = FailureWindowLimiter(max_failures=2)
    monkeypatch.setattr(auth_module, "_login_failures", limiter)
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    lookup_calls = []
    async def blocked_auth(*args):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        await release.wait()
        return None
    async def missing_candidate(db, username):
        lookup_calls.append(username)
        return None
    monkeypatch.setattr(AuthService, "authenticate_user", staticmethod(blocked_auth))
    monkeypatch.setattr(AuthService, "get_user_by_username", staticmethod(missing_candidate))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local") as client:
        payload = {"username": "victim", "password": "wrong"}
        pending = [asyncio.create_task(client.post("/api/auth/login", json=payload)) for _ in range(2)]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert (await client.post("/api/auth/login", json=payload)).status_code == 429
            release.set()
            assert [r.status_code for r in await asyncio.gather(*pending)] == [401, 401]
            assert not limiter._pending
            assert lookup_calls == ["victim", "victim"]
        finally:
            release.set()
            await asyncio.gather(*pending, return_exceptions=True)
