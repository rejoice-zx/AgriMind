"""Bootstrap/account-seeding security (MERGE-BOOT-01 / NEW-005).

Default startup must not create internet-usable weak demo accounts: seeding admin/
expert/farmer is an explicit opt-in (ALLOW_DEMO_SEEDING) and never happens in production.
A privileged initial admin comes from explicit secure injection and fails closed when
only the username is configured; repeated startup must not reset an existing password.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.database as dbmod
from app.database import init_db
from app.models.user import User


@pytest_asyncio.fixture
async def temp_init(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", sessions)

    async def _noop_seed(*args, **kwargs):
        return 0

    monkeypatch.setattr(
        "app.services.seed_agriculture_knowledge.seed_tomato_knowledge",
        _noop_seed, raising=False)

    async def usernames():
        async with sessions() as s:
            return set((await s.scalars(select(User.username))).all())

    try:
        yield sessions, usernames
    finally:
        await engine.dispose()


DEMO = {"admin", "expert", "farmer"}


@pytest.mark.asyncio
async def test_no_demo_accounts_without_explicit_optin(temp_init, monkeypatch):
    monkeypatch.delenv("ALLOW_DEMO_SEEDING", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    await init_db()
    _sessions, usernames = temp_init
    assert not (DEMO & await usernames())


@pytest.mark.asyncio
async def test_production_never_seeds_demo_even_when_opted_in(temp_init, monkeypatch):
    monkeypatch.setenv("ALLOW_DEMO_SEEDING", "true")
    monkeypatch.setenv("APP_ENV", "production")
    await init_db()
    _sessions, usernames = temp_init
    assert not (DEMO & await usernames())


@pytest.mark.asyncio
async def test_initial_admin_missing_password_refuses_startup(temp_init, monkeypatch):
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "root_admin")
    monkeypatch.delenv("INITIAL_ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        await init_db()


@pytest.mark.asyncio
async def test_initial_admin_from_env_then_idempotent(temp_init, monkeypatch):
    monkeypatch.delenv("ALLOW_DEMO_SEEDING", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "root_admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "a-strong-bootstrap-secret")

    await init_db()
    sessions, _u = temp_init
    async with sessions() as s:
        admin = (await s.execute(select(User).where(User.username == "root_admin"))).scalar_one()
        assert admin.role == "admin" and admin.is_active
        first_hash = admin.password_hash

    # Repeated startup must not reset the existing password.
    await init_db()
    async with sessions() as s:
        again = (await s.execute(select(User).where(User.username == "root_admin"))).scalar_one()
        assert again.password_hash == first_hash


@pytest.mark.asyncio
async def test_default_startup_leaves_no_weak_admin_login(temp_init, monkeypatch):
    """User-observable acceptance: with default config the seeded weak admin cannot log in."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.auth import router as auth_router
    from app.database import get_db

    monkeypatch.delenv("ALLOW_DEMO_SEEDING", raising=False)
    monkeypatch.delenv("INITIAL_ADMIN_USERNAME", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    await init_db()

    sessions, _usernames = temp_init
    app = FastAPI()
    app.include_router(auth_router)

    async def override_db():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 401
