"""Auth-endpoint rate limits (P2-AUTH-BOUNDS / DS-13).

Login failures and guest-token issuance get their own budgets, separate from the
global per-IP bucket. Keys stay `request.client.host`, so the existing client-IP
interpretation is unchanged; the proxy/shared-bucket question is NOT addressed
here (it needs real proxy evidence, which is out of scope).
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.database import get_db
from app.middleware.rate_limit import (
    GUEST_MAX_ISSUES,
    LOGIN_MAX_FAILURES,
    FailureWindowLimiter,
    IssueWindowLimiter,
)

LOGIN_IP = "198.51.100.7"
GUEST_IP = "198.51.100.9"
OTHER_IP = "198.51.100.11"


def test_failure_limiter_blocks_at_the_cap_and_reset_clears_it():
    limiter = FailureWindowLimiter(max_failures=3, window_seconds=60)
    assert limiter.is_blocked("ip") is False
    for _ in range(3):
        limiter.record_failure("ip")
    assert limiter.is_blocked("ip") is True
    limiter.reset("ip")
    assert limiter.is_blocked("ip") is False


def test_failure_limiter_window_expires(monkeypatch):
    limiter = FailureWindowLimiter(max_failures=2, window_seconds=10)
    clock = [1000.0]
    monkeypatch.setattr("app.middleware.rate_limit.time.monotonic", lambda: clock[0])
    limiter.record_failure("ip")
    limiter.record_failure("ip")
    assert limiter.is_blocked("ip") is True
    clock[0] += 11
    assert limiter.is_blocked("ip") is False


def test_issue_limiter_bounds_issuance_per_key():
    limiter = IssueWindowLimiter(max_issues=2, window_seconds=60)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is True  # keys are independent


def test_limiters_do_not_share_state():
    first = FailureWindowLimiter(max_failures=1, window_seconds=60)
    second = FailureWindowLimiter(max_failures=1, window_seconds=60)
    first.record_failure("ip")
    assert first.is_blocked("ip") is True
    assert second.is_blocked("ip") is False


def _auth_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_module.router)
    app.dependency_overrides[get_db] = lambda: None
    return app


@pytest.mark.asyncio
async def test_guest_issuance_is_budgeted_over_http():
    app = _auth_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, client=(GUEST_IP, 1234)), base_url="http://local"
    ) as client:
        codes = [
            (await client.post("/api/auth/guest")).status_code
            for _ in range(GUEST_MAX_ISSUES + 1)
        ]
    assert codes[:GUEST_MAX_ISSUES] == [200] * GUEST_MAX_ISSUES
    assert codes[-1] == 429


@pytest.mark.asyncio
async def test_guest_budget_is_not_shared_with_other_clients():
    app = _auth_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, client=(GUEST_IP, 1234)), base_url="http://local"
    ) as client:
        assert (await client.post("/api/auth/guest")).status_code == 429  # exhausted above
    async with AsyncClient(
        transport=ASGITransport(app=app, client=(OTHER_IP, 1234)), base_url="http://local"
    ) as client:
        assert (await client.post("/api/auth/guest")).status_code == 200


@pytest.mark.asyncio
async def test_login_failure_budget_and_success_reset(monkeypatch):
    """Over HTTP: a successful account login cannot erase the aggregate IP budget.

    Note the deliberate trade-off: once the budget is exhausted the client is
    refused before authentication runs, so it stays locked for the window even
    with the right password. That is the intended lockout semantics (a success
    cannot bypass an active lockout); "success resets" applies to the normal
    case of a few typos followed by a correct sign-in.
    """
    app = _auth_app()
    monkeypatch.setattr(auth_module, "_login_failures", FailureWindowLimiter())
    state = {"user": None}
    lookup_calls = []

    async def _authenticate(db, username, password):
        return state["user"]

    async def _missing_candidate(db, username):
        lookup_calls.append(username)
        return None

    monkeypatch.setattr(auth_module.AuthService, "authenticate_user", staticmethod(_authenticate))
    # Failed authentication now checks registration status before returning 401.
    monkeypatch.setattr(auth_module.AuthService, "get_user_by_username", staticmethod(_missing_candidate))
    monkeypatch.setattr(auth_module.AuthService, "update_last_login",
                        staticmethod(lambda db, user: _noop()))
    monkeypatch.setattr(auth_module.AuthService, "create_tokens",
                        staticmethod(lambda uid, un, subject: ("access", "refresh", 60)))
    monkeypatch.setattr(auth_module.AuthService, "resolve_guest_visitor_id",
                        staticmethod(lambda db, token: _noop()))
    monkeypatch.setattr(auth_module.LegacyIdentityService, "migrate_guest_conversations",
                        staticmethod(lambda **kwargs: _noop()))

    payload = {"username": "someone", "password": "wrong"}
    async with AsyncClient(
        transport=ASGITransport(app=app, client=(LOGIN_IP, 1234)), base_url="http://local"
    ) as client:
        # A few typos, then the correct password: the counter must be cleared.
        typos = [
            (await client.post("/api/auth/login", json=payload)).status_code
            for _ in range(LOGIN_MAX_FAILURES - 2)
        ]
        user = auth_module.User(username="someone", password_hash="x",
                                role="customer", is_active=True)
        user.id = 1
        user.created_at = datetime.now(timezone.utc)
        state["user"] = user
        signed_in = await client.post("/api/auth/login", json=payload)

        # Success clears only account-specific failures, not the IP failures.
        state["user"] = None
        failed_again = [
            (await client.post("/api/auth/login", json=payload)).status_code
            for _ in range(2)
        ]
        blocked = await client.post("/api/auth/login", json=payload)

    assert typos == [401] * (LOGIN_MAX_FAILURES - 2)
    assert signed_in.status_code == 200, signed_in.text
    assert failed_again == [401] * 2
    assert blocked.status_code == 429
    assert lookup_calls == ["someone"] * LOGIN_MAX_FAILURES


async def _noop(*args, **kwargs):
    return None
