"""Guest-conversation claim must be bound to a signed guest proof (DS-02 / DS-29).

Knowing a visitor_id, device_id or conversation id is not ownership. Migration must be
authorized by a verified guest access token; the self-reported device/session/ID values
may only narrow the verified visitor's own conversations, never expand the set.
"""
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import router as auth_router
from app.database import get_db
from app.services.auth_service import AuthService
from tests.legacy_fixtures import LegacyRows


@pytest_asyncio.fixture
async def client(test_session: AsyncSession):
    app = FastAPI()
    app.include_router(auth_router)

    async def override():
        yield test_session

    app.dependency_overrides[get_db] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


async def _new_customer(session, name):
    return await AuthService.create_user(
        db=session, username=name, password="pw123456", role="customer",
        display_name=name, is_active=True,
    )


@pytest.mark.asyncio
async def test_login_migration_requires_signed_guest_token(client, test_session):
    await _new_customer(test_session, "claim_login_user")
    victim = await LegacyRows.create_conversation(
        test_session, title="victim", customer_id="visitor_victim",
        temp_session_id="visitor_victim", temp_device_id="dev_shared")

    # Self-reported visitor_id + device_id + conversation id, but NO signed guest proof.
    resp = await client.post("/api/auth/login", json={
        "username": "claim_login_user", "password": "pw123456",
        "visitor_id": "visitor_victim", "guest_device_id": "dev_shared",
        "guest_conversation_ids": [victim.id],
    })
    assert resp.status_code == 200
    still = await LegacyRows.get_conversation(test_session, victim.id)
    assert still.customer_id == "visitor_victim"


@pytest.mark.asyncio
async def test_register_customer_migration_requires_signed_guest_token(client, test_session):
    victim = await LegacyRows.create_conversation(
        test_session, title="victim2", customer_id="visitor_victim2")

    resp = await client.post("/api/auth/register-customer", json={
        "username": "claim_reg_user", "password": "pw123456",
        "display_name": "Claim Reg", "phone": "13800000001", "guest_conversation_ids": [victim.id],
    })
    assert resp.status_code == 201
    still = await LegacyRows.get_conversation(test_session, victim.id)
    assert still.customer_id == "visitor_victim2"


@pytest.mark.asyncio
async def test_forged_device_id_cannot_claim_another_visitors_conversation(client, test_session):
    await _new_customer(test_session, "claim_login_user3")
    victim = await LegacyRows.create_conversation(
        test_session, title="victim3", customer_id="visitor_victim3",
        temp_session_id="visitor_victim3", temp_device_id="dev_shared")

    # A valid guest token for a DIFFERENT visitor must not reach the victim's conversation.
    guest_token = AuthService.create_access_token({"sub": "visitor_attacker", "role": "guest"})
    resp = await client.post("/api/auth/login", json={
        "username": "claim_login_user3", "password": "pw123456",
        "guest_token": guest_token, "guest_device_id": "dev_shared",
    })
    assert resp.status_code == 200
    still = await LegacyRows.get_conversation(test_session, victim.id)
    assert still.customer_id == "visitor_victim3"


@pytest.mark.asyncio
async def test_signed_guest_token_migrates_only_its_own_conversations(client, test_session):
    await _new_customer(test_session, "claim_login_user4")
    own = await LegacyRows.create_conversation(
        test_session, title="own", customer_id="visitor_claimer",
        temp_session_id="visitor_claimer", temp_device_id="dev_shared")
    other = await LegacyRows.create_conversation(
        test_session, title="other", customer_id="visitor_other4",
        temp_session_id="visitor_other4", temp_device_id="dev_shared")

    guest_token = AuthService.create_access_token({"sub": "visitor_claimer", "role": "guest"})
    resp = await client.post("/api/auth/login", json={
        "username": "claim_login_user4", "password": "pw123456",
        "guest_token": guest_token, "guest_device_id": "dev_shared",
    })
    assert resp.status_code == 200
    owner = f"user_{resp.json()['user']['id']}"
    assert (await LegacyRows.get_conversation(test_session, own.id)).customer_id == owner
    assert (await LegacyRows.get_conversation(test_session, other.id)).customer_id == "visitor_other4"


@pytest.mark.asyncio
async def test_legacy_guest_prefix_subject_is_migrated(client, test_session):
    await _new_customer(test_session, "claim_login_user5")
    legacy = await LegacyRows.create_conversation(
        test_session, title="legacy", customer_id="guest_legacy_old")

    guest_token = AuthService.create_access_token({"sub": "guest_legacy_old", "role": "guest"})
    resp = await client.post("/api/auth/login", json={
        "username": "claim_login_user5", "password": "pw123456", "guest_token": guest_token,
    })
    assert resp.status_code == 200
    owner = f"user_{resp.json()['user']['id']}"
    assert (await LegacyRows.get_conversation(test_session, legacy.id)).customer_id == owner


@pytest.mark.asyncio
async def test_registered_token_cannot_authorize_a_guest_claim(client, test_session):
    user = await _new_customer(test_session, "claim_login_user6")
    victim = await LegacyRows.create_conversation(
        test_session, title="victim6", customer_id="visitor_victim6")

    access, _refresh, _exp = AuthService.create_tokens(user.id, user.username, user.auth_subject)
    resp = await client.post("/api/auth/login", json={
        "username": "claim_login_user6", "password": "pw123456", "guest_token": access,
    })
    assert resp.status_code == 200
    assert (await LegacyRows.get_conversation(test_session, victim.id)).customer_id == "visitor_victim6"


@pytest.mark.asyncio
async def test_real_guest_issuance_then_register_migrates_own_history(client, test_session):
    # 1) real signed guest token from the public endpoint (not a forged one)
    guest = await client.post("/api/auth/guest")
    assert guest.status_code == 200
    guest_token = guest.json()["access_token"]

    # This row represents history that predates retirement of the chat API.
    conv = await LegacyRows.create_conversation(test_session, title='guest history', customer_id=guest.json()['visitor_id'])
    conv_id = conv.id

    # 3) register a customer carrying the signed guest token
    reg = await client.post("/api/auth/register-customer", json={
        "username": "acc_real_guest", "password": "pw123456", "display_name": "Acc", "phone": "13800000002",
        "guest_token": guest_token,
    })
    assert reg.status_code == 201

    assert (await LegacyRows.get_conversation(test_session, conv_id)).customer_id == f"user_{reg.json()['user']['id']}"


@pytest.mark.asyncio
async def test_forged_claim_without_token_does_not_move_victim_history(client, test_session):
    # the victim's real guest conversation
    guest = await client.post("/api/auth/guest")
    victim_token = guest.json()["access_token"]
    victim_visitor = guest.json()["visitor_id"]
    conv = await LegacyRows.create_conversation(test_session, title='victim', customer_id=victim_visitor)
    victim_conv_id = conv.id

    # attacker knows the victim's visitor_id/device/ids but holds NO signed guest token
    reg = await client.post("/api/auth/register-customer", json={
        "username": "acc_attacker", "password": "pw123456", "display_name": "Atk", "phone": "13800000003",
        "visitor_id": victim_visitor, "guest_device_id": "whatever",
        "guest_conversation_ids": [victim_conv_id],
    })
    assert reg.status_code == 201
    still = await LegacyRows.get_conversation(test_session, victim_conv_id)
    assert still.customer_id == victim_visitor
