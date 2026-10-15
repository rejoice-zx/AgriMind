"""Protected bytes and durable inference receipts with real FK-enabled independent sessions."""
import asyncio
import struct
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func, update, delete
from app.database import get_db
from app.models import User, Field, DiseaseRecord, AgriConversation, AgriTurn
from app.models.diagnosis_media import DiagnosisMedia, DiagnosisRequest
from app.api.dependencies import get_current_user
from app.api.expert_workbench import router, legacy_media_router
from app.api.farming_records import router as records_router
from app.api.agri_agent import router as agent_router
from app.services.agent_service import AgriTools
from app.services.expert_service import ExpertWorkbenchService
from app.services.diagnosis_media_service import DiagnosisMediaService
from app.services.diagnosis_request_service import DiagnosisRequestService
from app.schemas.expert import VisualDiagnosisRequest
from app.utils.time import utcnow
from tests.test_disease_review_lifecycle import env  # file DB, persisted users, FK ON


@pytest_asyncio.fixture
async def media_env(env, monkeypatch):
    app = FastAPI()
    for routes in (router, legacy_media_router, records_router, agent_router): app.include_router(routes)
    caller = [env.users["owner"]]
    async def database():
        async with env.sessions() as db: yield db
    async def authenticated(): return caller[0]
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = authenticated
    calls = []
    async def vision(**kwargs):
        calls.append(kwargs)
        return {"suspected_disease": "待鉴别", "confidence": 0.4, "need_expert": True, "features_detected": ["测试特征"], "severity": "unknown"}
    monkeypatch.setattr(AgriTools, "diagnose_crop_image", vision)
    client = AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")
    image = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 400, 400) + b"\x08\x02\0\0\0" + b"fixture" * 600
    response = await client.post("/api/diagnosis/upload", files={"file": ("leaf.png", image, "text/html")})
    assert response.status_code == 200
    yield SimpleNamespace(**env.__dict__, app=app, caller=caller, client=client, image=image, url=response.json()["url"], calls=calls)
    await client.aclose()


def body(env, **overrides):
    return {"request_id": str(uuid4()), "image_url": env.url, "field_id": env.fields["owner"].id, **overrides}


async def count(db, model): return await db.scalar(select(func.count()).select_from(model))


@pytest.mark.asyncio
async def test_upload_private_and_experts_only_view_linked_business_images(media_env):
    env = media_env
    served = await env.client.get(env.url)
    assert served.status_code == 200 and served.content == env.image
    assert served.headers["content-type"] == "image/png"
    assert "no-store" in served.headers["cache-control"] and served.headers["x-content-type-options"] == "nosniff"
    filename = env.url.rsplit("/", 1)[1]
    assert (await env.client.get("/static/uploads/diagnosis/" + filename)).status_code == 404
    for name in ("other", "cs", "a", "admin"):
        env.caller[0] = env.users[name]
        assert (await env.client.get(env.url)).status_code == 404
    env.caller[0] = env.users["owner"]
    result = await env.client.post("/api/diagnosis/visual-diagnose", json=body(env))
    assert result.status_code == 200
    for name in ("a", "admin"):
        env.caller[0] = env.users[name]
        assert (await env.client.get(env.url)).status_code == 200
    env.app.dependency_overrides.pop(get_current_user)
    assert (await env.client.get(env.url)).status_code == 401


@pytest.mark.asyncio
async def test_foreign_reference_cannot_manufacture_permission_or_call_models(media_env):
    env = media_env
    env.caller[0] = env.users["other"]
    manual = await env.client.post(f'/api/fields/{env.fields["other"].id}/disease-records', json={"disease_name": "巡查", "image_url": env.url})
    assert manual.status_code == 404
    assert (await env.client.post("/api/agent/decide", json={"query": "核对照片", "image_url": env.url, "enable_multimodal": False})).status_code == 404
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=body(env, field_id=env.fields["other"].id))).status_code == 404
    assert not env.calls
    async with env.sessions() as db:
        assert await count(db, DiagnosisRequest) == 0 and await count(db, AgriTurn) == 0
        assert await count(db, DiseaseRecord) == 1  # initial fixture only


@pytest.mark.asyncio
async def test_expert_cannot_link_patient_photo_into_another_farm(media_env):
    env = media_env
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=body(env))).status_code == 200
    env.caller[0] = env.users["a"]
    assert (await env.client.get(env.url)).status_code == 200
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=body(env, field_id=env.fields["other"].id))).status_code == 404
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=body(env))).status_code == 200
    assert len(env.calls) == 2


@pytest.mark.asyncio
async def test_legacy_files_require_existing_owned_record_and_unknown_files_stay_unavailable(media_env):
    env = media_env
    path = DiagnosisMediaService.path("legacy.png", True); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(env.image)
    async with env.sessions() as db:
        db.add(DiseaseRecord(field_id=env.fields["owner"].id, disease_name="历史病例", image_url="/static/uploads/diagnosis/legacy.png"))
        await db.commit()
    assert (await env.client.get("/static/uploads/diagnosis/legacy.png")).status_code == 200
    for name in ("other", "cs"):
        env.caller[0] = env.users[name]
        assert (await env.client.get("/static/uploads/diagnosis/legacy.png")).status_code == 404
    env.caller[0] = env.users["a"]
    assert (await env.client.get("/static/uploads/diagnosis/legacy.png")).status_code == 200
    orphan = DiagnosisMediaService.path("orphan.png", True); orphan.write_bytes(env.image)
    assert (await env.client.get("/static/uploads/diagnosis/orphan.png")).status_code == 404
    for url in ("/api/diagnosis/media/../../outside.png", "/static/uploads/diagnosis/a%2Fb.png"):
        assert (await env.client.get(url)).status_code != 200


@pytest.mark.asyncio
async def test_lost_response_replay_status_and_changed_payload_never_rediagnose(media_env):
    env = media_env; payload = body(env)
    first = await env.client.post("/api/diagnosis/visual-diagnose", json=payload)
    assert first.status_code == 200 and not first.json()["replayed"]
    replay = await env.client.post("/api/diagnosis/visual-diagnose", json=payload)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert first.json()["disease_record_id"] == replay.json()["disease_record_id"]
    state = (await env.client.get(f'/api/diagnosis/requests/{payload["request_id"]}')).json()
    assert state["status"] == "completed" and state["result"]["replayed"]
    changed = await env.client.post("/api/diagnosis/visual-diagnose", json={**payload, "symptoms_hint": "不同内容"})
    assert changed.status_code == 409 and len(env.calls) == 1
    env.caller[0] = env.users["other"]
    assert (await env.client.get(f'/api/diagnosis/requests/{payload["request_id"]}')).status_code == 404
    async with env.sessions() as db:
        assert await count(db, DiagnosisRequest) == 1 and await count(db, DiseaseRecord) == 2


@pytest.mark.asyncio
async def test_model_failure_has_durable_failed_receipt_no_case_and_explicit_retry(media_env, monkeypatch):
    from app.services.vision_service import VisionService
    env = media_env; payload = body(env)
    async def failure(**kwargs):
        env.calls.append(kwargs)
        return VisionService.failed_result(env.url, "未提供", "timeout", "等待模型响应超时")
    monkeypatch.setattr(AgriTools, "diagnose_crop_image", failure)
    first = await env.client.post("/api/diagnosis/visual-diagnose", json=payload)
    assert first.status_code == 200
    data = first.json()
    assert data["diagnosis_status"] == "failed" and data["error_code"] == "timeout"
    assert data["ticket_id"] is None and data["disease_record_id"] is None
    replay = await env.client.post("/api/diagnosis/visual-diagnose", json=payload)
    assert replay.json()["replayed"] and replay.json()["error_code"] == "timeout"
    state = (await env.client.get(f'/api/diagnosis/requests/{payload["request_id"]}')).json()
    assert state["status"] == "failed" and state["result"]["error_code"] == "timeout"
    assert len(env.calls) == 1
    async with env.sessions() as db:
        assert await count(db, DiseaseRecord) == 1  # fixture only, no failure case
        assert (await db.get(DiagnosisRequest, payload["request_id"])).status == "failed"
    async def recovered(**kwargs):
        env.calls.append(kwargs)
        return {"suspected_disease": "待鉴别", "confidence": .4, "need_expert": True}
    monkeypatch.setattr(AgriTools, "diagnose_crop_image", recovered)
    retried = await env.client.post("/api/diagnosis/visual-diagnose", json={**payload, "request_id": str(uuid4())})
    assert retried.json()["diagnosis_status"] == "completed" and retried.json()["ticket_id"] is not None
    assert len(env.calls) == 2
    async with env.sessions() as db:
        assert await count(db, DiseaseRecord) == 2


@pytest.mark.asyncio
async def test_legacy_offline_receipt_is_presented_as_failed_without_rewriting_history(media_env):
    env = media_env; payload = body(env)
    first = (await env.client.post("/api/diagnosis/visual-diagnose", json=payload)).json()
    async with env.sessions() as db:
        job = await db.get(DiagnosisRequest, payload["request_id"])
        old_result = {key: value for key, value in job.result.items() if key not in {"diagnosis_status", "error_code", "error_message"}}
        old_result["suspected_disease"] = "视觉模型离线/网络受阻"
        job.result = old_result
        await db.commit()
    replay = (await env.client.post("/api/diagnosis/visual-diagnose", json=payload)).json()
    assert replay["diagnosis_status"] == "failed" and replay["error_code"] == "legacy_failure"
    assert replay["ticket_id"] == first["ticket_id"] and len(env.calls) == 1
    state = (await env.client.get(f'/api/diagnosis/requests/{payload["request_id"]}')).json()
    assert state["status"] == "failed"
    async with env.sessions() as db:
        assert (await db.get(DiagnosisRequest, payload["request_id"])).result == old_result
        assert await count(db, DiseaseRecord) == 2


@pytest.mark.asyncio
async def test_concurrent_same_request_infers_once_and_status_reads_do_not_call_models(media_env, monkeypatch):
    env = media_env; started, release = asyncio.Event(), asyncio.Event()
    async def slow(**kwargs):
        env.calls.append(kwargs); started.set(); await release.wait()
        return {"suspected_disease": "待鉴别", "confidence": 0.4, "need_expert": True}
    monkeypatch.setattr(AgriTools, "diagnose_crop_image", slow)
    payload = body(env)
    first = asyncio.create_task(env.client.post("/api/diagnosis/visual-diagnose", json=payload))
    await asyncio.wait_for(started.wait(), 10)
    duplicate = asyncio.create_task(env.client.post("/api/diagnosis/visual-diagnose", json=payload))
    try:
        state = (await env.client.get(f'/api/diagnosis/requests/{payload["request_id"]}')).json()
        assert state["status"] == "pending" and len(env.calls) == 1
    finally:
        release.set()
    first_response, duplicate_response = await asyncio.gather(first, duplicate)
    assert first_response.status_code == duplicate_response.status_code == 200
    assert first_response.json()['disease_record_id'] == duplicate_response.json()['disease_record_id']
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=payload)).status_code == 200
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_case_and_same_failed_request_is_not_rerun(media_env, monkeypatch):
    env = media_env; original = ExpertWorkbenchService.visual_diagnose
    async def fail_after_case(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated result receipt failure")
    monkeypatch.setattr(ExpertWorkbenchService, "visual_diagnose", fail_after_case)
    payload = body(env)
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=payload)).status_code == 500
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=payload)).status_code == 409
    assert len(env.calls) == 1
    state = (await env.client.get(f'/api/diagnosis/requests/{payload["request_id"]}')).json()
    assert state["status"] == "failed" and state["result"] is None
    async with env.sessions() as db:
        assert await count(db, DiseaseRecord) == 1
        job = await db.get(DiagnosisRequest, payload["request_id"])
        assert job.record_id is None and job.result is None


@pytest.mark.asyncio
async def test_stale_pending_is_needs_review_and_never_automatically_reclaimed(media_env):
    env = media_env; payload = body(env)
    async with env.sessions() as db:
        db.add(DiagnosisRequest(id=payload["request_id"], owner_id=env.users["owner"].id,
            owner_subject=env.users["owner"].auth_subject, payload=VisualDiagnosisRequest(**payload).model_dump(mode="json", exclude={"request_id"}),
            field_id=env.fields["owner"].id, field_created_at=env.fields["owner"].created_at,
            status="pending", created_at=utcnow() - timedelta(minutes=11)))
        await db.commit()
    assert (await env.client.get(f'/api/diagnosis/requests/{payload["request_id"]}')).json()["status"] == "needs_review"
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=payload)).status_code == 409
    assert env.calls == []


@pytest.mark.asyncio
async def test_deleted_field_and_reused_ids_cannot_make_cached_reply_a_new_case(media_env):
    env = media_env; payload = body(env)
    result = (await env.client.post("/api/diagnosis/visual-diagnose", json=payload)).json()
    async with env.sessions() as db:
        field = await db.get(Field, env.fields["owner"].id)
        farm_id, field_id = field.farm_id, field.id
        await db.execute(delete(Field).where(Field.id == field_id)); await db.commit()
        db.add(Field(id=field_id, farm_id=farm_id, name="重建档案")); await db.flush()
        db.add(DiseaseRecord(id=result["disease_record_id"], field_id=field_id, disease_name="新的无关病例")); await db.commit()
    assert (await env.client.get(f'/api/diagnosis/requests/{payload["request_id"]}')).status_code == 409
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=payload)).status_code == 409
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_deleted_uploader_and_reused_numeric_account_do_not_inherit_unlinked_file(media_env):
    env = media_env
    async with env.sessions() as db:
        old = User(username="isolated_uploader", password_hash="unused", role="customer", is_active=True)
        db.add(old); await db.commit(); old_id = old.id
    env.caller[0] = old
    uploaded = (await env.client.post("/api/diagnosis/upload", files={"file": ("leaf.png", env.image, "image/png")})).json()
    async with env.sessions() as db:
        await db.execute(delete(User).where(User.id == old_id)); await db.commit()
        replacement = User(id=old_id, username="replacement_uploader", password_hash="unused", role="customer", is_active=True)
        db.add(replacement); await db.commit()
    env.caller[0] = replacement
    assert (await env.client.get(uploaded["url"])).status_code == 404


@pytest.mark.asyncio
async def test_new_private_url_is_readable_by_actual_vision_parser(media_env, monkeypatch):
    import base64
    from app.services.vision_service import VisionService
    env = media_env; posted = []
    class Response:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": '{"suspected_disease":"待鉴别","confidence":0.4,"need_expert":true}'}}]}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs): posted.append(kwargs["json"]); return Response()
    monkeypatch.setattr("app.services.vision_service.httpx.AsyncClient", Client)
    result = await VisionService.diagnose_leaf_image(env.url, api_key="fixture-only")
    assert result["image_url"] == env.url and len(posted) == 1
    encoded = posted[0]["messages"][0]["content"][1]["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == env.image


@pytest.mark.asyncio
async def test_production_factory_isolates_uncommitted_sessions_and_preserves_foreign_keys(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.database import Base, create_database_engine
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'isolation.db').as_posix()}")
    async with engine.begin() as connection: await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as writer:
        writer.add(User(username="uncommitted", password_hash="unused", role="customer"))
        await writer.flush()
        async with sessions() as reader:
            assert await count(reader, User) == 0
            assert await reader.scalar(text("PRAGMA foreign_keys")) == 1
        await writer.commit()
    async with sessions() as verifier: assert await count(verifier, User) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_inference_does_not_leave_case_and_cannot_be_retried_implicitly(media_env, monkeypatch):
    env = media_env; started = asyncio.Event()
    async def blocked(**kwargs):
        env.calls.append(kwargs); started.set(); await asyncio.Event().wait()
    monkeypatch.setattr(AgriTools, "diagnose_crop_image", blocked)
    payload = body(env)
    async def run():
        async with env.sessions() as db:
            return await DiagnosisRequestService.run(db, VisualDiagnosisRequest(**payload), env.users["owner"])
    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=payload)).status_code == 409
    async with env.sessions() as db:
        assert (await db.get(DiagnosisRequest, payload["request_id"])).status == "failed"
        assert await count(db, DiseaseRecord) == 1
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_disable_during_inference_blocks_result_and_clears_case_transaction(media_env, monkeypatch):
    env = media_env
    async def disable(**kwargs):
        env.calls.append(kwargs)
        async with env.sessions() as db:
            await db.execute(update(User).where(User.id == env.users["owner"].id).values(is_active=False))
            await db.commit()
        return {"suspected_disease": "待鉴别", "confidence": 0.4, "need_expert": True}
    monkeypatch.setattr(AgriTools, "diagnose_crop_image", disable)
    payload = body(env)
    assert (await env.client.post("/api/diagnosis/visual-diagnose", json=payload)).status_code == 409
    async with env.sessions() as db:
        assert await count(db, DiseaseRecord) == 1
        assert (await db.get(DiagnosisRequest, payload["request_id"])).status == "failed"
