"""Admin deletion uses real FK cascades and preserves durable request identities."""
import pytest
from sqlalchemy import select, func, update
from app.api.dependencies import get_current_user
from app.models import DiseaseRecord, DiseaseReview, ExpertCase, KnowledgeItem, User
from app.models.diagnosis_media import DiagnosisRequest, DiagnosisMedia
from app.services.disease_review_service import DiseaseReviewService
from tests.test_disease_review_lifecycle import env, payload as review_payload
from tests.test_diagnosis_media_and_requests import media_env, body


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "other", "a", "cs"])
async def test_only_admin_can_delete_even_owned_records(media_env, actor):
    setup = media_env
    setup.caller[0] = setup.users[actor]
    response = await setup.client.delete(f"/api/diagnosis/records/{setup.record_id}")
    assert response.status_code == 403
    async with setup.sessions() as db:
        assert await db.get(DiseaseRecord, setup.record_id) is not None


@pytest.mark.asyncio
async def test_unauthenticated_cannot_delete(media_env):
    setup = media_env
    setup.app.dependency_overrides.pop(get_current_user)
    assert (await setup.client.delete(f"/api/diagnosis/records/{setup.record_id}")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["preliminary", "pending_review", "in_review", "expert_reviewed"])
async def test_delete_cascades_feedback_preserves_knowledge_media_and_blocks_old_receipts(media_env, state):
    setup = media_env
    request = body(setup)
    result = (await setup.client.post("/api/diagnosis/visual-diagnose", json=request)).json()
    record_id = result["disease_record_id"]
    knowledge_id = None
    async with setup.sessions() as db:
        if state == "preliminary":
            await db.execute(update(DiseaseRecord).where(DiseaseRecord.id == record_id).values(status=state))
            await db.commit()
        if state in {"in_review", "expert_reviewed"}:
            await DiseaseReviewService.claim(db, record_id, setup.users["a"])
        if state == "expert_reviewed":
            resolved = await DiseaseReviewService.resolve(db, record_id, setup.users["a"], review_payload(1))
            knowledge_id = resolved.knowledge_item_id
    setup.caller[0] = setup.users["admin"]
    deleted = await setup.client.delete(f"/api/diagnosis/records/{record_id}")
    assert deleted.status_code == 204 and not deleted.content
    assert (await setup.client.get(f"/api/diagnosis/records/{record_id}")).status_code == 404
    assert (await setup.client.get(f"/api/expert/tickets/{record_id}")).status_code == 404
    assert (await setup.client.delete(f"/api/diagnosis/records/{record_id}")).status_code == 404
    listing = (await setup.client.get("/api/diagnosis/records")).json()
    assert listing["total"] == 1 and listing["items"][0]["ticket_id"] == setup.record_id
    async with setup.sessions() as db:
        assert await db.get(DiseaseRecord, record_id) is None
        assert await db.get(DiseaseReview, record_id) is None
        assert await db.scalar(select(func.count()).select_from(ExpertCase).where(ExpertCase.disease_record_id == record_id)) == 0
        receipt = await db.get(DiagnosisRequest, request["request_id"])
        assert receipt.record_id is None and receipt.result["disease_record_id"] == record_id
        assert await db.get(DiagnosisMedia, setup.url.rsplit("/", 1)[-1]) is not None
        if knowledge_id: assert await db.get(KnowledgeItem, knowledge_id) is not None
        assert await db.get(DiseaseRecord, setup.record_id) is not None
    setup.caller[0] = setup.users["owner"]
    assert (await setup.client.post("/api/diagnosis/visual-diagnose", json=request)).status_code == 409
    assert (await setup.client.get(f'/api/diagnosis/requests/{request["request_id"]}')).status_code == 409
    assert len(setup.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [{"role": "expert"}, {"is_active": False}])
async def test_stale_admin_identity_cannot_delete_after_permission_change(media_env, values):
    setup = media_env
    setup.caller[0] = setup.users["admin"]
    async with setup.sessions() as db:
        await db.execute(update(User).where(User.id == setup.users["admin"].id).values(**values))
        await db.commit()
    assert (await setup.client.delete(f"/api/diagnosis/records/{setup.record_id}")).status_code == 403
    async with setup.sessions() as db:
        assert await db.get(DiseaseRecord, setup.record_id) is not None
