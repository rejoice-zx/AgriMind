"""Resource authorization regressions with real permissions and isolated FK data."""
from datetime import date
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.agri_agent import router
from app.api.dependencies import get_current_user
from app.database import Base, get_db
from app.models.crop import CropPlanting
from app.models.farm import Farm
from app.models.field import Field
from app.models.user import User
from app.models.weather import WeatherSnapshot
from app.schemas.agent import AgentDecisionRequest
from app.services.agent_service import AgriAgentService, AgriToolRegistry, AgriTools
from app.services.weather_service import WeatherService


@pytest_asyncio.fixture
async def resource_env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        assert await db.scalar(text("PRAGMA foreign_keys")) == 1
        users = {}
        for name, role in (("owner", "customer"), ("other", "customer"),
                           ("admin", "admin"), ("expert", "expert"), ("cs", "cs")):
            users[name] = User(username=name, password_hash="fictional", role=role, is_active=True)
        db.add_all(users.values())
        await db.flush()
        farms = [Farm(name="Owned farm", owner_id=users["owner"].id),
                 Farm(name="Other farm", owner_id=users["other"].id)]
        db.add_all(farms)
        await db.flush()
        fields = {}
        for name, farm in (("own", farms[0]), ("other", farms[1]), ("mismatch", farms[0])):
            fields[name] = Field(name=name, farm_id=farm.id, area=1.0, latitude=36.88, longitude=118.74)
        db.add_all(fields.values())
        await db.flush()
        crops = {name: CropPlanting(field_id=field.id, crop="番茄", planting_date=date.today(),
                                    growth_stage="苗期", status="active")
                 for name, field in fields.items()}
        db.add_all(crops.values())
        await db.commit()
        yield SimpleNamespace(db=db, users=users, fields=fields, crops=crops)
    await engine.dispose()


@pytest.fixture
def observed_tools(monkeypatch):
    """Keep registry, resource queries and snapshot persistence real; stub external data."""
    calls = {"registry": [], "weather": 0, "llm": 0, "knowledge": 0}
    execute = AgriToolRegistry.execute_tool

    async def observed_execute(tool_name, arguments, db):
        calls["registry"].append((tool_name, dict(arguments)))
        return await execute(tool_name, arguments, db)

    async def weather(**_):
        calls["weather"] += 1
        from app.utils.time import utcnow
        return {"current": {"temperature": 21.0, "humidity": 60, "rainfall": 0, "wind_speed": 1,
                            "timestamp": utcnow().isoformat(), "weather": "晴", "source": "Test"},
                "forecasts": [], "spray_safety": {"is_suitable_to_spray": True}}

    async def knowledge(*args, **kwargs):
        calls["knowledge"] += 1
        return {"items": [], "confidence": 0.8}

    async def synthesize(**_):
        calls["llm"] += 1
        return {"conclusion": "Test decision", "can_proceed": False, "confidence": 0.8,
                "risks": [], "recommendations": ["Test advice"], "need_expert": False}

    monkeypatch.setattr(AgriToolRegistry, "execute_tool", observed_execute)
    monkeypatch.setattr(WeatherService, "get_real_weather", weather)
    monkeypatch.setattr(AgriTools, "search_agri_knowledge", knowledge)
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", synthesize)
    return calls


def resource_request(env, field=None, crop=None):
    values = {"query": "番茄可以施肥吗？"}
    if field is not None:
        values["field_id"] = env.fields[field].id if isinstance(field, str) else field
    if crop is not None:
        values["crop_planting_id"] = env.crops[crop].id if isinstance(crop, str) else crop
    return AgentDecisionRequest(**values)


async def assert_no_side_effects(env, calls):
    assert calls == {"registry": [], "weather": 0, "llm": 0, "knowledge": 0}
    assert await env.db.scalar(select(func.count()).select_from(WeatherSnapshot)) == 0
    assert not env.db.new and not env.db.dirty and not env.db.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("caller,field,crop,expected", [
    ("cs", "own", None, 403),
    ("cs", None, "own", 403),
    ("other", "own", None, 403),
    ("other", None, "own", 403),
    ("owner", "own", "other", 403),
    ("owner", "own", "mismatch", 400),
    (None, "own", None, 401),
    (None, None, "own", 401),
    ("owner", 99999, None, 404),
    ("owner", 0, None, 404),
    ("owner", -1, None, 404),
    ("owner", None, 99999, 404),
    ("owner", None, 0, 404),
    ("owner", None, -1, 404),
    ("owner", "own", 99999, 404),
])
async def test_service_rejects_before_tools(resource_env, observed_tools, caller, field, crop, expected):
    env = resource_env
    user = env.users[caller] if caller else None
    with pytest.raises(HTTPException) as exc:
        await AgriAgentService.decide(env.db, resource_request(env, field, crop), current_user=user)
    assert exc.value.status_code == expected
    await assert_no_side_effects(env, observed_tools)


@pytest.mark.asyncio
async def test_old_resource_signature_cannot_bypass_authorization(resource_env, observed_tools):
    with pytest.raises(HTTPException) as exc:
        await AgriAgentService.decide(resource_env.db, resource_request(resource_env, "own"))
    assert exc.value.status_code == 401
    await assert_no_side_effects(resource_env, observed_tools)


@pytest.mark.asyncio
async def test_inactive_service_caller_rejected(resource_env, observed_tools):
    env = resource_env
    inactive = User(id=env.users["owner"].id, role="admin", is_active=False)
    with pytest.raises(HTTPException) as exc:
        await AgriAgentService.decide(env.db, resource_request(env, "own"), current_user=inactive)
    assert exc.value.status_code == 401
    await assert_no_side_effects(env, observed_tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["owner", "admin", "expert"])
@pytest.mark.parametrize("crop_only", [False, True])
async def test_owner_and_existing_staff_scope_succeed(resource_env, observed_tools, caller, crop_only):
    env = resource_env
    request = resource_request(env, None if crop_only else "own", "own")
    decision = await AgriAgentService.decide(env.db, request, current_user=env.users[caller])
    field_calls = [args for name, args in observed_tools["registry"] if name == "get_field_info"]
    assert field_calls == [{"field_id": env.fields["own"].id, "crop_planting_id": request.crop_planting_id}]
    assert any("own" in evidence["content"] for evidence in decision.evidences)
    assert observed_tools["weather"] == observed_tools["llm"] == 1
    assert await env.db.scalar(select(func.count()).select_from(WeatherSnapshot)) == 1
    snapshot = (await env.db.scalars(select(WeatherSnapshot))).one()
    assert snapshot.field_id == env.fields["own"].id
    assert request.field_id == (None if crop_only else env.fields["own"].id)


@pytest.mark.asyncio
async def test_resource_free_service_call_stays_compatible(resource_env, observed_tools):
    decision = await AgriAgentService.decide(resource_env.db, AgentDecisionRequest(query="番茄是什么？"))
    assert decision.decision_type == "GENERAL"
    assert not any(name in ("get_field_info", "get_farming_history")
                   for name, _ in observed_tools["registry"])
    assert observed_tools["llm"] == 1
    assert await resource_env.db.scalar(select(func.count()).select_from(WeatherSnapshot)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("caller,expected", [("owner", 200), ("other", 403), (None, 401)])
async def test_api_passes_real_caller_and_blocks_anonymous(resource_env, observed_tools, caller, expected):
    env = resource_env
    app = FastAPI()
    app.include_router(router)

    async def database():
        yield env.db

    app.dependency_overrides[get_db] = database
    if caller is not None:
        async def authenticated_user():
            return env.users[caller]
        app.dependency_overrides[get_current_user] = authenticated_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/agent/decide", json={
            "query": "番茄可以施肥吗？", "field_id": env.fields["own"].id})
    assert response.status_code == expected
    if expected == 200:
        assert observed_tools["weather"] == observed_tools["llm"] == 1
    else:
        await assert_no_side_effects(env, observed_tools)
