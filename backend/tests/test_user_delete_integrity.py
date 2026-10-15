"""User deletion keeps agricultural data and anonymizes conversations (DS-03 / D2).

D2 policy: a deleted user's conversations are anonymized (customer_id -> deleted_user_*),
their agent references are cleared (SET NULL equivalent), but farms are preserved —
deletion is refused while the user still owns farms.
"""
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AdminRequired
from app.api.users import router as users_router
from app.database import get_db
from app.models.conversation import Conversation, Message
from app.models.farm import Farm
from app.models.user import User
from app.services.auth_service import AuthService
from tests.legacy_fixtures import LegacyRows


@pytest_asyncio.fixture
async def admin_client(test_session: AsyncSession):
    admin = User(username="del_admin", password_hash="x", role="admin", is_active=True)
    test_session.add(admin)
    await test_session.commit()

    app = FastAPI()
    app.include_router(users_router)

    async def override_db():
        yield test_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[AdminRequired] = lambda: admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client, admin
    app.dependency_overrides.clear()


async def _customer(session, name):
    return await AuthService.create_user(
        db=session, username=name, password="pw123456", role="customer",
        display_name=name, is_active=True)


@pytest.mark.asyncio
async def test_delete_anonymizes_conversations_and_keeps_messages(admin_client, test_session):
    client, _admin = admin_client
    customer = await _customer(test_session, "del_customer")
    conv = await LegacyRows.create_conversation(
        test_session, title="owned", customer_id=f"user_{customer.id}")
    test_session.add(Message(conversation_id=conv.id, role="user", content="keep me"))
    await test_session.commit()
    cid = customer.id

    resp = await client.delete(f"/api/users/{cid}")
    assert resp.status_code == 204

    assert await test_session.get(User, cid) is None
    refreshed = await test_session.get(Conversation, conv.id)
    assert refreshed is not None and refreshed.customer_id == f"deleted_user_{cid}"
    msgs = (await test_session.scalars(
        select(Message).where(Message.conversation_id == conv.id))).all()
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_delete_clears_agent_reference(admin_client, test_session):
    client, _admin = admin_client
    agent = await AuthService.create_user(
        db=test_session, username="del_agent", password="pw123456", role="cs",
        display_name="Agent", is_active=True)
    conv = await LegacyRows.create_conversation(
        test_session, title="handled", customer_id="visitor_x")
    await test_session.execute(
        update(Conversation).where(Conversation.id == conv.id).values(human_agent_id=agent.id))
    await test_session.commit()
    aid = agent.id

    resp = await client.delete(f"/api/users/{aid}")
    assert resp.status_code == 204

    refreshed = await test_session.get(Conversation, conv.id)
    assert refreshed is not None and refreshed.human_agent_id is None


@pytest.mark.asyncio
async def test_delete_refused_when_user_owns_farm(admin_client, test_session):
    client, _admin = admin_client
    owner = await _customer(test_session, "del_farm_owner")
    test_session.add(Farm(name="keep me", owner_id=owner.id))
    await test_session.commit()
    oid = owner.id

    resp = await client.delete(f"/api/users/{oid}")
    assert resp.status_code == 409

    assert await test_session.get(User, oid) is not None
    farms = (await test_session.scalars(select(Farm).where(Farm.owner_id == oid))).all()
    assert len(farms) == 1
