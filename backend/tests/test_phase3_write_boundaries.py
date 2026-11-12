"""Phase-3 write-boundary regressions (BUG-006, BUG-030).

Experts read any field and answer through review receipts; only the field owner or an
administrator writes the farm's own history (farming, disease, weather snapshot, plan
confirmation). Real file-backed SQLite sessions, HTTP through the actual routers.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func, update
from app.database import get_db
from app.models import Farm, Field, FarmingRecord, DiseaseRecord, WeatherSnapshot
from app.api.dependencies import get_current_user
from app.api.farming_records import router as records_router
from app.services.agri_plan_service import AgriPlanService
from app.utils.time import utcnow
from tests.test_disease_review_lifecycle import env  # noqa: F401  (file DB, persisted users, FK ON)


@pytest_asyncio.fixture
async def http(env):
    app = FastAPI()
    app.include_router(records_router)
    caller = [env.users["owner"]]

    async def database():
        async with env.sessions() as db:
            yield db

    async def authenticated():
        return caller[0]

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = authenticated
    client = AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")
    yield SimpleNamespace(env=env, caller=caller, client=client)
    await client.aclose()


def spray(field_id):
    return {"field_id": field_id, "record_type": "spraying", "record_date": "2026-10-01",
            "input_name": "测试药剂", "safety_interval_days": 30}


async def count(env, model):
    async with env.sessions() as db:
        return await db.scalar(select(func.count()).select_from(model))


@pytest.mark.asyncio
async def test_expert_cannot_write_farming_records_into_a_farmers_field(http):
    env, field_id = http.env, http.env.fields["owner"].id
    http.caller[0] = env.users["a"]
    compat = await http.client.post("/api/farming-records", json=spray(field_id))
    nested = await http.client.post(f"/api/fields/{field_id}/farming-records",
                                    json={k: v for k, v in spray(field_id).items() if k != "field_id"})
    assert compat.status_code == 403 and nested.status_code == 403
    assert await count(env, FarmingRecord) == 0
    # Read access for experts is unchanged.
    assert (await http.client.get(f"/api/farming-records/field/{field_id}")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_owner_and_admin_still_write_farming_records(http, role):
    http.caller[0] = http.env.users[role]
    response = await http.client.post("/api/farming-records", json=spray(http.env.fields["owner"].id))
    assert response.status_code in (200, 201), response.text
    assert await count(http.env, FarmingRecord) == 1


@pytest.mark.asyncio
async def test_expert_cannot_write_disease_records_or_weather_snapshots(http):
    env, field_id = http.env, http.env.fields["owner"].id
    http.caller[0] = env.users["a"]
    before = await count(env, DiseaseRecord)
    disease = await http.client.post(f"/api/fields/{field_id}/disease-records", json={"disease_name": "伪造病害"})
    weather = await http.client.post(f"/api/fields/{field_id}/weather", json={"temperature": 20})
    assert disease.status_code == 403 and weather.status_code == 403
    assert await count(env, DiseaseRecord) == before
    assert await count(env, WeatherSnapshot) == 0


@pytest.mark.asyncio
async def test_archived_field_rejects_manual_disease_record(http):
    env, field_id = http.env, http.env.fields["owner"].id
    async with env.sessions() as db:
        await db.execute(update(Field).where(Field.id == field_id).values(archived_at=utcnow()))
        await db.commit()
    before = await count(env, DiseaseRecord)
    response = await http.client.post(f"/api/fields/{field_id}/disease-records", json={"disease_name": "归档后补录"})
    assert response.status_code == 409
    assert await count(env, DiseaseRecord) == before


@pytest.mark.asyncio
async def test_plan_confirmation_requires_owner_or_admin_but_experts_may_read(env):
    field_id = env.fields["owner"].id
    async with env.sessions() as db:
        expert = env.users["a"]
        assert (await AgriPlanService._field(db, field_id, expert.id, expert.role)).id == field_id
        with pytest.raises(HTTPException) as denied:
            await AgriPlanService._field(db, field_id, expert.id, expert.role, write=True)
        assert denied.value.status_code == 403
        owner = env.users["owner"]
        assert (await AgriPlanService._field(db, field_id, owner.id, owner.role, write=True)).id == field_id


def _launcher():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "run.py"
    spec = importlib.util.spec_from_file_location("phase3_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_respects_environment_from_dotenv_and_process(tmp_path, monkeypatch):
    launcher = _launcher()
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    assert launcher.configured_environment(tmp_path) is None  # nothing configured -> launcher may default
    (tmp_path / ".env").write_text("APP_ENV=production\n", encoding="utf-8")
    assert launcher.configured_environment(tmp_path) == "production"
    monkeypatch.setenv("ENV", "staging")
    assert launcher.configured_environment(tmp_path) == "staging"


@pytest.mark.asyncio
async def test_expert_case_knowledge_is_not_retrievable_until_reviewed(env):
    from app.core.knowledge_contract import candidate_eligible
    from app.models import KnowledgeItem
    from app.services.disease_review_service import DiseaseReviewService
    from app.schemas.expert import ExpertResolveRequest
    from app.utils.time import utcnow
    async with env.sessions() as db:
        record = await db.get(DiseaseRecord, env.record_id)
        knowledge_id = await DiseaseReviewService.add_knowledge(db, record, ExpertResolveRequest(
            confirmed_disease="测试病害", treatment_suggestion="补充照片后复核", is_knowledge_candidate=1), env.users["a"])
        item = await db.get(KnowledgeItem, knowledge_id)
        assert item.source_type == "unverified" and item.is_current == 1
        assert not candidate_eligible(item, utcnow().date())
        await db.rollback()


@pytest.mark.asyncio
async def test_unverified_pesticide_reference_endpoint_is_gone(http):
    response = await http.client.get("/api/farming-records/pesticide-standards")
    assert response.status_code in (404, 405, 422)  # route removed; path may now hit /farming-records/{id}
