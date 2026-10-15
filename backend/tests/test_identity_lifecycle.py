"""Account lifetime regression checks with in-memory SQLite and real JWT/routes."""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from app.api.auth import router as auth_router
from app.api.users import router as users_router
from app.models.user import User
from app.models.conversation import Conversation
from app.migrations.migrate_auth_subject import migrate_auth_subjects
from app.services.auth_service import AuthService
from tests.active_identity_fixtures import identity_env, access


@pytest.mark.asyncio
async def test_deleted_numeric_id_reuse_never_revives_old_credentials(identity_env):
    env = identity_env
    async with env.sessions() as db:
        old = User(username="retired_account", password_hash="fixture", role="customer", is_active=True)
        db.add(old)
        await db.commit()
        old_id, old_subject = old.id, old.auth_subject
        stale_access, stale_refresh, _ = AuthService.create_tokens(old.id, old.username, old.auth_subject)
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://local") as client:
        response = await client.delete(f"/api/users/{old_id}",
                                      headers={"Authorization": "Bearer " + access(env, "admin")})
        assert response.status_code == 204
        async with env.sessions() as db:
            replacement = User(username="replacement_admin", password_hash="fixture", role="admin", is_active=True)
            db.add(replacement)
            await db.commit()
            assert replacement.id == old_id
            assert replacement.auth_subject != old_subject
            current, _, _ = AuthService.create_tokens(replacement.id, replacement.username, replacement.auth_subject)
        assert (await client.get("/api/users", headers={"Authorization": "Bearer " + stale_access})).status_code == 401
        assert (await client.post("/api/auth/refresh", json={"refresh_token": stale_refresh})).status_code == 401
        assert (await client.get("/api/users", headers={"Authorization": "Bearer " + current})).status_code == 200
        assert not any(path.startswith(('/api/chat', '/api/human')) for path in env.app.openapi()['paths'])
        for path in ('/api/chat/conversations', '/api/human/conversations'):
            assert (await client.get(path, headers={'Authorization': 'Bearer ' + current})).status_code == 404


@pytest.mark.asyncio
async def test_legacy_registered_credentials_are_explicitly_invalid(identity_env):
    env = identity_env
    legacy_access, legacy_refresh = [factory({"sub": str(env.ids["admin"]), "username": "admin"})
        for factory in (AuthService.create_access_token, AuthService.create_refresh_token)]
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://local") as client:
        for route in ("/api/users", "/api/agent/conversations"):
            assert (await client.get(route, headers={"Authorization": "Bearer " + legacy_access})).status_code == 401
        assert (await client.post("/api/auth/refresh", json={"refresh_token": legacy_refresh})).status_code == 401


@pytest.mark.asyncio
async def test_disable_then_reenable_does_not_revive_old_credentials(identity_env):
    env = identity_env
    async with env.sessions() as db:
        await db.execute(update(Conversation).values(title="Fixture conversation"))
        await db.commit()
    old_access, old_refresh, _ = AuthService.create_tokens(env.ids["customer"], "customer", env.subjects["customer"])
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://local") as client:
        admin = {"Authorization": "Bearer " + access(env, "admin")}
        for enabled in (False, True):
            assert (await client.put(f"/api/users/{env.ids['customer']}", json={"is_active": enabled}, headers=admin)).status_code == 200
            assert (await client.get("/api/agent/conversations", headers={"Authorization": "Bearer " + old_access})).status_code == 401
            assert (await client.post("/api/auth/refresh", json={"refresh_token": old_refresh})).status_code == 401
        async with env.sessions() as db:
            user = await db.get(User, env.ids["customer"])
            current, _, _ = AuthService.create_tokens(user.id, user.username, user.auth_subject)
        assert (await client.get("/api/agent/conversations", headers={"Authorization": "Bearer " + current})).status_code == 200


@pytest.mark.asyncio
async def test_old_database_subject_upgrade_is_unique_and_idempotent():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL)"))
            await conn.execute(text("INSERT INTO users(id,username) VALUES (1,'first'),(2,'second')"))
            await conn.run_sync(migrate_auth_subjects)
            before = (await conn.execute(text("SELECT id,auth_subject FROM users ORDER BY id"))).all()
            assert len({row.auth_subject for row in before}) == 2
            assert all(len(row.auth_subject) == 36 for row in before)
            await conn.run_sync(migrate_auth_subjects)
            assert (await conn.execute(text("SELECT id,auth_subject FROM users ORDER BY id"))).all() == before
            with pytest.raises(IntegrityError):
                await conn.execute(text("INSERT INTO users(id,username) VALUES (3,'invalid')"))
            with pytest.raises(IntegrityError):
                await conn.execute(text("INSERT INTO users(id,username,auth_subject) VALUES (3,'duplicate',:subject)"),
                                   {"subject": before[0].auth_subject})
    finally:
        await engine.dispose()
