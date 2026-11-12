"""Phase-3 auth/admin regressions (BUG-016, BUG-017, BUG-018, BUG-020, BUG-050)."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import Column, Integer, MetaData, Table, select
from sqlalchemy.ext.asyncio import create_async_engine
from app.api.users import update_user, reset_password, UpdateUserRequest, ResetPasswordRequest, CreateUserRequest
from app.schemas.auth import CustomerRegisterRequest
from app.services.auth_service import AuthService
from app.utils.time import UTCDateTime


@pytest.mark.parametrize("password", ["a" * 73, "番" * 25])
def test_passwords_over_72_utf8_bytes_are_rejected_before_bcrypt(password):
    for schema in (ResetPasswordRequest, ):
        with pytest.raises(ValidationError):
            schema(password=password)
    with pytest.raises(ValidationError):
        CustomerRegisterRequest(username="farmer01", password=password, display_name="农户", phone="13800000000")
    ResetPasswordRequest(password="番" * 24)  # 72 bytes exactly is accepted


@pytest.mark.asyncio
async def test_utc_datetime_normalizes_aware_values_on_write():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    table = Table("t", MetaData(), Column("id", Integer, primary_key=True), Column("at", UTCDateTime))
    async with engine.begin() as conn:
        await conn.run_sync(table.metadata.create_all)
        shanghai = timezone(timedelta(hours=8))
        await conn.execute(table.insert().values(id=1, at=datetime(2026, 1, 1, 8, 0, tzinfo=shanghai)))
        value = (await conn.execute(select(table.c.at))).scalar_one()
    await engine.dispose()
    assert value == datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_unknown_username_still_runs_password_check(test_session, monkeypatch):
    calls = []
    original = AuthService.verify_password

    def counting(plain, hashed):
        calls.append(hashed)
        return original(plain, hashed)

    monkeypatch.setattr(AuthService, "verify_password", staticmethod(counting))
    assert await AuthService.authenticate_user(test_session, "nobody-here", "whatever-password") is None
    assert len(calls) == 1  # constant-ish timing: one bcrypt comparison even for unknown accounts


@pytest.mark.asyncio
async def test_create_user_defaults_to_customer_not_legacy_cs(test_session):
    user = await AuthService.create_user(test_session, username="default-role", password="secret-123", display_name="d")
    assert user.role == "customer"


async def _admins(test_session):
    root = await AuthService.create_user(test_session, username="admin", password="secret-123", display_name="root", role="admin", is_active=True)
    second = await AuthService.create_user(test_session, username="admin2", password="secret-123", display_name="a2", role="admin", is_active=True)
    return root, second


@pytest.mark.asyncio
async def test_ordinary_admin_cannot_reset_demote_or_disable_super_admin(test_session):
    root, second = await _admins(test_session)
    subject = root.auth_subject
    with pytest.raises(HTTPException) as reset:
        await reset_password(root.id, ResetPasswordRequest(password="takeover-123"), db=test_session, current_user=second)
    with pytest.raises(HTTPException) as demote:
        await update_user(root.id, UpdateUserRequest(role="customer"), db=test_session, current_user=second)
    with pytest.raises(HTTPException) as disable:
        await update_user(root.id, UpdateUserRequest(is_active=False), db=test_session, current_user=second)
    assert reset.value.status_code == demote.value.status_code == disable.value.status_code == 403
    await test_session.refresh(root)
    assert root.auth_subject == subject and root.role == "admin" and root.is_active


@pytest.mark.asyncio
async def test_super_admin_can_still_manage_other_admins(test_session):
    root, second = await _admins(test_session)
    result = await reset_password(second.id, ResetPasswordRequest(password="new-secret-1"), db=test_session, current_user=root)
    assert result.id == second.id
    updated = await update_user(second.id, UpdateUserRequest(role="expert"), db=test_session, current_user=root)
    assert updated.role == "expert"
