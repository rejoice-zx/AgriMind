"""Real file-backed sessions verify ownership, races, immutable feedback and rollback."""
import asyncio
from datetime import date
from types import SimpleNamespace
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, func, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import Base, get_db, create_database_engine
from app.models import User, Farm, Field, CropPlanting, DiseaseRecord, DiseaseReview, ExpertCase, KnowledgeItem
from app.api.dependencies import get_current_user
from app.api.expert_workbench import router
from app.api.farming_records import router as records_router
from app.api.users import update_user, delete_user, UpdateUserRequest
from app.schemas.expert import ExpertResolveRequest, VisualDiagnosisRequest
from app.services.disease_review_service import DiseaseReviewService as Reviews
from app.services.expert_service import ExpertWorkbenchService as Workbench
from app.services.agent_service import AgriTools


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'reviews.db').as_posix()}")
    @event.listens_for(engine.sync_engine, "connect")
    def fks(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        users = {}
        for name, role in [("owner", "customer"), ("other", "customer"), ("a", "expert"), ("b", "expert"), ("admin", "admin"), ("cs", "cs")]:
            users[name] = User(username=name, role=role, display_name=name, password_hash="unused", is_active=True)
            db.add(users[name])
        await db.flush()
        fields = {}
        for name in ("owner", "other", "cs"):
            farm = Farm(name=name, owner_id=users[name].id); db.add(farm); await db.flush()
            fields[name] = Field(name=name, farm_id=farm.id); db.add(fields[name]); await db.flush()
        crop = CropPlanting(field_id=fields["other"].id, crop="私有作物", planting_date=date(2026, 1, 1))
        db.add(crop)
        record = DiseaseRecord(field_id=fields["owner"].id, disease_name="待鉴别", status="pending_review", diagnosis_method="ai_vision")
        db.add(record); await db.commit()
        yield SimpleNamespace(sessions=sessions, users=users, fields=fields, crop_id=crop.id, record_id=record.id)
    await engine.dispose()


def payload(candidate=0):
    return ExpertResolveRequest(confirmed_disease="资料不足，待鉴别", treatment_suggestion="补充叶背和整株照片后进一步复核", notes="当前信息不足以确诊", is_knowledge_candidate=candidate)


async def counts(db):
    return [await db.scalar(select(func.count()).select_from(model)) for model in (ExpertCase, KnowledgeItem)]


@pytest.mark.asyncio
async def test_concurrent_claim_persists_one_owner(env):
    async def claim(name):
        async with env.sessions() as db:
            try: await Reviews.claim(db, env.record_id, env.users[name]); return name
            except HTTPException as error:
                assert error.status_code == 409
                return None
    winners = [name for name in await asyncio.gather(claim("a"), claim("b")) if name]
    assert len(winners) == 1
    async with env.sessions() as db:
        detail = await Workbench.get_ticket_detail(db, env.record_id)
        assert detail.status == "in_review"
        assert detail.expert_id == env.users[winners[0]].id
        assert not detail.claim_available
        assert detail.current_weather is None and detail.field_soil_type is None and detail.ai_features == []
        await Reviews.claim(db, env.record_id, env.users[winners[0]])


@pytest.mark.asyncio
async def test_assignment_required_and_resolution_immutable_with_atomic_case(env):
    async with env.sessions() as db:
        with pytest.raises(HTTPException) as error: await Reviews.resolve(db, env.record_id, env.users["a"], payload(1))
        assert error.value.status_code == 409
        await db.rollback()
        await Reviews.claim(db, env.record_id, env.users["a"])
        with pytest.raises(HTTPException) as error: await Reviews.resolve(db, env.record_id, env.users["b"], payload(1))
        assert error.value.status_code == 409
        await db.rollback()
        first = await Reviews.resolve(db, env.record_id, env.users["a"], payload(1))
        replay = await Reviews.resolve(db, env.record_id, env.users["a"], payload(1))
        assert first == replay and first.knowledge_item_id
        assert await counts(db) == [1, 1]
        modified = payload(1).model_copy(update={"notes": "修改内容"})
        with pytest.raises(HTTPException) as error: await Reviews.resolve(db, env.record_id, env.users["a"], modified)
        assert error.value.status_code == 409
        assert await counts(db) == [1, 1]
    async with env.sessions() as db:
        record = await Reviews.record(db, env.record_id)
        feedback = Reviews.feedback(record)
        assert feedback.workflow_verified and feedback.expert_name == "a"
        assert feedback.confirmed_disease == payload().confirmed_disease
        assert feedback.treatment_suggestion == payload().treatment_suggestion
        knowledge = await db.get(KnowledgeItem, first.knowledge_item_id)
        assert knowledge.authority_level == "L4" and knowledge.source_type == "expert"


@pytest.mark.asyncio
async def test_concurrent_identical_resolution_creates_one_case(env):
    async with env.sessions() as db: await Reviews.claim(db, env.record_id, env.users["a"])
    async def resolve():
        async with env.sessions() as db: return await Reviews.resolve(db, env.record_id, env.users["a"], payload(1))
    a, b = await asyncio.gather(resolve(), resolve())
    assert a.expert_case_id == b.expert_case_id and a.knowledge_item_id == b.knowledge_item_id
    async with env.sessions() as db: assert await counts(db) == [1, 1]


@pytest.mark.asyncio
async def test_knowledge_failure_rolls_back_whole_resolution(env, monkeypatch):
    async with env.sessions() as db: await Reviews.claim(db, env.record_id, env.users["a"])
    original = Reviews.add_knowledge
    async def fail_after_insert(*args):
        await original(*args)
        raise RuntimeError("storage failure")
    monkeypatch.setattr(Reviews, "add_knowledge", fail_after_insert)
    async with env.sessions() as db:
        with pytest.raises(RuntimeError, match="storage failure"): await Reviews.resolve(db, env.record_id, env.users["a"], payload(1))
    async with env.sessions() as db:
        record = await Reviews.record(db, env.record_id)
        assert record.status == "in_review" and record.expert_result is None
        assert record.review.resolved_payload is None and record.review.result_case_id is None
        assert await counts(db) == [0, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["disable", "demote", "delete"])
async def test_account_lifecycle_requeues_and_rejects_stale_expert(env, action):
    async with env.sessions() as db: await Reviews.claim(db, env.record_id, env.users["a"])
    async with env.sessions() as db:
        if action == "delete": await delete_user(env.users["a"].id, db, env.users["admin"])
        else: await update_user(env.users["a"].id, UpdateUserRequest(**({"is_active": False} if action == "disable" else {"role": "cs"})), db, env.users["admin"])
    async with env.sessions() as db:
        record = await Reviews.record(db, env.record_id)
        assert record.status == "pending_review" and record.review.assigned_expert_id is None
        with pytest.raises(HTTPException) as error: await Reviews.resolve(db, env.record_id, env.users["a"], payload())
        assert error.value.status_code == 403
        await db.rollback()
        await Reviews.claim(db, env.record_id, env.users["b"])
        await Reviews.resolve(db, env.record_id, env.users["b"], payload())
        assert await counts(db) == [1, 0]


@pytest.mark.asyncio
async def test_old_unassigned_review_claimable_but_historical_result_never_rewritten(env):
    async with env.sessions() as db:
        await db.execute(update(DiseaseRecord).where(DiseaseRecord.id == env.record_id).values(status="in_review"))
        await db.commit()
        assert (await Workbench.get_ticket_detail(db, env.record_id)).claim_available
        await Reviews.claim(db, env.record_id, env.users["a"])
        old = DiseaseRecord(field_id=env.fields["owner"].id, disease_name="历史名称", status="expert_reviewed", expert_result="历史用户上报内容")
        db.add(old); await db.commit()
        detail = await Workbench.get_ticket_detail(db, old.id)
        assert not detail.claim_available and not detail.feedback.workflow_verified
        with pytest.raises(HTTPException) as error: await Reviews.claim(db, old.id, env.users["a"])
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_owner_feedback_api_scope_manual_input_and_legacy_bypass(env):
    app = FastAPI(); app.include_router(router); app.include_router(records_router)
    current = [env.users["owner"]]
    async def database():
        async with env.sessions() as db: yield db
    async def authenticated(): return current[0]
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = authenticated
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for name in ("other", "cs"):
            current[0] = env.users[name]
            assert (await client.get("/api/diagnosis/records")).json()["total"] == 0
            for method, url in [("GET", f"/api/diagnosis/records/{env.record_id}"), ("POST", f"/api/diagnosis/records/{env.record_id}/request-review")]:
                assert (await client.request(method, url)).status_code == 403
            assert (await client.get("/api/expert/tickets")).status_code == 403
        current[0] = env.users["owner"]
        assert (await client.get("/api/diagnosis/records")).json()["total"] == 1
        url = f'/api/fields/{env.fields["owner"].id}/disease-records'
        for forged in [{"expert_result": "伪造专家意见"}, {"status": "expert_reviewed"}, {"diagnosis_method": "ai_vision"}, {"diagnosis_confidence": 0.99}]:
            assert (await client.post(url, json={"disease_name": "巡查", **forged})).status_code == 422
        assert (await client.post(url, json={"disease_name": "巡查", "crop_planting_id": env.crop_id})).status_code == 400
        manual = await client.post(url, json={"disease_name": "巡查"})
        assert manual.status_code == 201
        rid = manual.json()["id"]
        transfer_url = f"/api/diagnosis/records/{rid}/request-review"
        assert (await client.post(transfer_url)).json()["status"] == "pending_review"
        assert (await client.post(transfer_url)).json()["status"] == "pending_review"
        current[0] = env.users["a"]
        body = {"disease_record_id": rid, **payload().model_dump()}
        assert (await client.post("/api/expert/cases", json=body)).status_code == 409
        assert (await client.post(f"/api/expert/tickets/{rid}/claim")).status_code == 200
        first = await client.post("/api/expert/cases", json=body)
        second = await client.post("/api/expert/cases", json=body)
        assert first.status_code == second.status_code == 201 and first.json()["id"] == second.json()["id"]
        page = (await client.get("/api/expert/tickets?offset=1&limit=2")).json()
        assert page["total"] == 2 and len(page["items"]) == 1 and page["items"][0]["ticket_id"] == env.record_id
        current[0] = env.users["owner"]
        detail = (await client.get(f"/api/diagnosis/records/{rid}")).json()
        assert detail["feedback"]["workflow_verified"] and detail["feedback"]["treatment_suggestion"] == payload().treatment_suggestion


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [None, True, "0.99", -1, 1.5])
async def test_invalid_model_scores_are_unknown_not_fabricated(env, monkeypatch, score):
    async def diagnose(**kwargs): return {"suspected_disease": "待鉴别", "confidence": score, "features_detected": ["实际模型特征", 7], "need_expert": False}
    monkeypatch.setattr(AgriTools, "diagnose_crop_image", diagnose)
    async with env.sessions() as db:
        result = await Workbench.visual_diagnose(db, VisualDiagnosisRequest(image_url="fixture.png", field_id=env.fields["owner"].id), env.users["owner"])
        assert result.confidence is None and result.need_expert and result.ticket_id
        assert result.crop == "未提供" and result.severity == "unknown"
        detail = await Workbench.get_ticket_detail(db, result.ticket_id)
        assert detail.ai_features == ["实际模型特征"] and detail.initial_confidence is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "transfer", "replace_lifetime"])
async def test_visual_persistence_rechecks_account_and_field_during_slow_model_call(env, monkeypatch, change):
    async def diagnose(**kwargs):
        async with env.sessions() as db:
            if change == "disable":
                await db.execute(update(User).where(User.id == env.users["owner"].id).values(is_active=False))
            elif change == "transfer":
                await db.execute(update(Farm).where(Farm.id == env.fields["owner"].farm_id).values(owner_id=env.users["other"].id))
            else:
                await db.execute(update(User).where(User.id == env.users["owner"].id).values(auth_subject="new-lifetime"))
            await db.commit()
        return {"suspected_disease": "待鉴别", "confidence": 0.3, "need_expert": True}
    monkeypatch.setattr(AgriTools, "diagnose_crop_image", diagnose)
    async with env.sessions() as db:
        with pytest.raises(HTTPException) as error:
            await Workbench.visual_diagnose(db, VisualDiagnosisRequest(image_url="fixture.png", field_id=env.fields["owner"].id), env.users["owner"])
        assert error.value.status_code == 409
    async with env.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(DiseaseRecord)) == 1
        assert await db.scalar(select(func.count()).select_from(DiseaseReview)) == 0


@pytest.mark.asyncio
async def test_completed_feedback_survives_expert_deletion_and_numeric_id_reuse(env):
    async with env.sessions() as db:
        await Reviews.claim(db, env.record_id, env.users["a"])
        first = await Reviews.resolve(db, env.record_id, env.users["a"], payload())
    async with env.sessions() as db:
        await delete_user(env.users["a"].id, db, env.users["admin"])
        replacement = User(id=env.users["a"].id, username="replacement", display_name="新账号", password_hash="unused", is_active=True, role="expert")
        db.add(replacement); await db.commit()
        detail = await Workbench.get_ticket_detail(db, env.record_id)
        assert detail.feedback.workflow_verified and detail.feedback.expert_name == "a"
        assert detail.expert_name == "a" and detail.expert_id is None
        assert detail.feedback.expert_case_id == first.expert_case_id
        with pytest.raises(HTTPException) as error: await Reviews.resolve(db, env.record_id, replacement, payload())
        assert error.value.status_code == 409
        assert await counts(db) == [1, 0]


@pytest.mark.asyncio
async def test_failed_retrieval_leaves_no_unreturned_visual_record(env, monkeypatch):
    async def diagnose(**kwargs): return {"suspected_disease": "模型疑似", "confidence": 0.9, "need_expert": False}
    async def fail(**kwargs): raise RuntimeError("retrieval unavailable")
    monkeypatch.setattr(AgriTools, "diagnose_crop_image", diagnose)
    monkeypatch.setattr(AgriTools, "search_agri_knowledge", fail)
    async with env.sessions() as db:
        with pytest.raises(RuntimeError, match="retrieval unavailable"):
            await Workbench.visual_diagnose(db, VisualDiagnosisRequest(image_url="fixture.png", field_id=env.fields["owner"].id), env.users["owner"])
        assert await db.scalar(select(func.count()).select_from(DiseaseRecord)) == 1
        assert await db.scalar(select(func.count()).select_from(DiseaseReview)) == 0
