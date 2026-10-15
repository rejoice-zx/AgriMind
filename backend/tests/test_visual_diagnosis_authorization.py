"""Visual diagnosis authorization and persistence contract, with no real model calls."""
from datetime import date
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.expert_workbench import router
from app.database import Base, get_db
from app.models.crop import CropPlanting
from app.models.disease_record import DiseaseRecord
from app.models.expert_case import ExpertCase
from app.models.farm import Farm
from app.models.field import Field
from app.models.user import User
from app.models.weather import WeatherSnapshot
from app.schemas.expert import VisualDiagnosisRequest
from app.services.agent_service import AgriTools
from app.services.auth_service import AuthService
from app.services.expert_service import ExpertWorkbenchService


@pytest_asyncio.fixture
async def diagnosis_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    statements = []

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def capture_statements(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.info["statements"] = statements
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def diagnosis_users(diagnosis_db):
    users = {}
    for name, role in (("owner", "customer"), ("other", "customer"),
                       ("admin", "admin"), ("expert", "expert"), ("cs", "cs")):
        users[name] = User(username=f"diagnosis_{name}", role=role,
                           password_hash="unused-fixture-hash", is_active=True)
    diagnosis_db.add_all(users.values())
    await diagnosis_db.commit()
    from pathlib import Path
    from uuid import uuid4
    from app.config import settings
    from app.models.diagnosis_media import DiagnosisMedia
    root = Path(settings.DATA_DIR) / "private_uploads" / "diagnosis"
    root.mkdir(parents=True, exist_ok=True)
    for user in users.values():
        filename = uuid4().hex + ".png"
        (root / filename).write_bytes(b"controlled-media-fixture")
        diagnosis_db.add(DiagnosisMedia(id=filename, owner_id=user.id, content_type="image/png", size_bytes=24, sha256="test"))
        user.fixture_image_url = "/api/diagnosis/media/" + filename
    await diagnosis_db.commit()
    return users


@pytest_asyncio.fixture
async def diagnosis_fields(diagnosis_db, diagnosis_users):
    return await create_diagnosis_fields(diagnosis_db, diagnosis_users)


async def create_diagnosis_fields(diagnosis_db, diagnosis_users):
    fields = {}
    for name in ("owner", "other"):
        farm = Farm(name=f"{name} farm", owner_id=diagnosis_users[name].id)
        diagnosis_db.add(farm)
        await diagnosis_db.flush()
        field = Field(name=f"{name} field", farm_id=farm.id, area=1)
        diagnosis_db.add(field)
        await diagnosis_db.flush()
        crop = CropPlanting(field_id=field.id, crop=f"{name} private crop",
                            planting_date=date(2026, 1, 1), status="active", area=1)
        diagnosis_db.add(crop)
        fields[name] = (field, crop)
    await diagnosis_db.commit()
    return fields


@pytest_asyncio.fixture
async def diagnosis_client(diagnosis_db):
    app = FastAPI()
    app.include_router(router)

    async def isolated_db():
        yield diagnosis_db

    app.dependency_overrides[get_db] = isolated_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def diagnosis_tools(monkeypatch):
    vision = AsyncMock(return_value={
        "suspected_disease": "疑似真实病斑", "confidence": 0.45,
        "features_detected": ["边缘模糊", "真实叶脉"], "severity": "未知",
        "need_expert": True, "description": "真实病叶需要专家鉴别",
    })
    knowledge = AsyncMock(return_value={"items": [{"answer": "fixture treatment"}]})
    monkeypatch.setattr(AgriTools, "diagnose_crop_image", vision)
    monkeypatch.setattr(AgriTools, "search_agri_knowledge", knowledge)
    return vision, knowledge


def headers(user):
    access = AuthService.create_access_token(data={"sub": str(user.id), "role": user.role, "account_subject": user.auth_subject})
    return {"Authorization": f"Bearer {access}"}


async def assert_no_records(session):
    for model in (DiseaseRecord, ExpertCase, WeatherSnapshot):
        assert (await session.scalar(select(func.count()).select_from(model))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin", "expert"])
@pytest.mark.parametrize("confidence", [0.45, 0.92])
async def test_authorized_owner_and_staff_persist(diagnosis_client, diagnosis_db,
                                                 diagnosis_users, diagnosis_fields,
                                                 diagnosis_tools, role, confidence):
    vision, knowledge = diagnosis_tools
    vision.return_value.update(confidence=confidence, need_expert=confidence < 0.7)
    field, crop = diagnosis_fields["owner"]
    response = await diagnosis_client.post("/api/diagnosis/visual-diagnose", json={
        "image_url": diagnosis_users[role].fixture_image_url, "field_id": field.id,
    }, headers=headers(diagnosis_users[role]))
    assert response.status_code == 200
    data = response.json()
    assert data["crop"] == crop.crop
    assert data["disease_record_id"] is not None
    record = await diagnosis_db.get(DiseaseRecord, data["disease_record_id"])
    assert record.field_id == field.id
    assert record.crop_planting_id == crop.id
    assert record.status == ("pending_review" if confidence < 0.7 else "preliminary")
    assert data["ticket_id"] == (record.id if confidence < 0.7 else None)
    assert (await diagnosis_db.scalar(select(func.count()).select_from(DiseaseRecord))) == 1
    vision.assert_awaited_once()
    assert vision.await_args.kwargs["crop"] == crop.crop
    assert knowledge.await_count == (0 if confidence < 0.7 else 1)


@pytest.mark.asyncio
async def test_customer_cannot_write_foreign_field(diagnosis_client, diagnosis_db,
                                                  diagnosis_users, diagnosis_fields,
                                                  diagnosis_tools):
    field, _ = diagnosis_fields["other"]
    diagnosis_db.info["statements"].clear()
    response = await diagnosis_client.post("/api/diagnosis/visual-diagnose", json={
        "image_url": diagnosis_users["owner"].fixture_image_url, "field_id": field.id,
    }, headers=headers(diagnosis_users["owner"]))
    assert response.status_code == 403
    assert not any("crop_plantings" in s for s in diagnosis_db.info["statements"])
    for tool in diagnosis_tools:
        tool.assert_not_awaited()
    await assert_no_records(diagnosis_db)


@pytest.mark.asyncio
@pytest.mark.parametrize("field_id", [0, -1, 999999])
async def test_invalid_id_cannot_fall_back_to_another_field(diagnosis_client, diagnosis_db,
                                                         diagnosis_users, diagnosis_fields,
                                                         diagnosis_tools, field_id):
    response = await diagnosis_client.post("/api/diagnosis/visual-diagnose", json={
        "image_url": "fixture.png", "field_id": field_id,
    }, headers=headers(diagnosis_users["owner"]))
    assert response.status_code == 404
    for tool in diagnosis_tools:
        tool.assert_not_awaited()
    await assert_no_records(diagnosis_db)


@pytest.mark.asyncio
async def test_resource_diagnosis_without_caller_is_rejected(diagnosis_db, diagnosis_fields,
                                                           diagnosis_tools):
    field, _ = diagnosis_fields["owner"]
    diagnosis_db.info["statements"].clear()
    with pytest.raises(HTTPException) as error:
        await ExpertWorkbenchService.visual_diagnose(diagnosis_db,
            VisualDiagnosisRequest(image_url="fixture.png", field_id=field.id))
    assert error.value.status_code == 401
    assert diagnosis_db.info["statements"] == []
    for tool in diagnosis_tools:
        tool.assert_not_awaited()
    await assert_no_records(diagnosis_db)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin", "expert", "cs"])
@pytest.mark.parametrize("resource_bound", [False, True])
async def test_inactive_service_caller_has_no_side_effects(diagnosis_db, diagnosis_fields,
                                                         diagnosis_users, diagnosis_tools,
                                                         role, resource_bound):
    user = diagnosis_users[role]
    user.is_active = False
    diagnosis_db.info["statements"].clear()
    field_id = diagnosis_fields["owner"][0].id if resource_bound else None
    with pytest.raises(HTTPException) as error:
        await ExpertWorkbenchService.visual_diagnose(diagnosis_db,
            VisualDiagnosisRequest(image_url="fixture.png", field_id=field_id), current_user=user)
    assert error.value.status_code == 401
    assert diagnosis_db.info["statements"] == []
    for tool in diagnosis_tools:
        tool.assert_not_awaited()
    await assert_no_records(diagnosis_db)


@pytest.mark.asyncio
async def test_service_also_checks_customer_ownership(diagnosis_db, diagnosis_fields,
                                                    diagnosis_users, diagnosis_tools):
    with pytest.raises(HTTPException) as error:
        await ExpertWorkbenchService.visual_diagnose(diagnosis_db,
            VisualDiagnosisRequest(image_url="fixture.png", field_id=diagnosis_fields["other"][0].id),
            current_user=diagnosis_users["owner"])
    assert error.value.status_code == 403
    for tool in diagnosis_tools:
        tool.assert_not_awaited()
    await assert_no_records(diagnosis_db)


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [0.45, 0.92])
@pytest.mark.parametrize("existing_fields", [False, True])
async def test_unselected_field_returns_nullable_ids_without_persistence(diagnosis_client,
        diagnosis_db, diagnosis_users, diagnosis_tools, confidence, existing_fields):
    if existing_fields:
        await create_diagnosis_fields(diagnosis_db, diagnosis_users)
    vision, _ = diagnosis_tools
    vision.return_value.update(confidence=confidence, need_expert=confidence < 0.7)
    diagnosis_db.info["statements"].clear()
    response = await diagnosis_client.post("/api/diagnosis/visual-diagnose", json={
        "image_url": diagnosis_users["owner"].fixture_image_url, "crop": "requested crop",
    }, headers=headers(diagnosis_users["owner"]))
    assert response.status_code == 200
    data = response.json()
    assert data["disease_record_id"] is None
    assert data["ticket_id"] is None
    assert data["need_expert"] is (confidence < 0.7)
    assert data["crop"] == "requested crop"
    assert "未保存病害记录或创建专家复核工单" in data["advice"]
    assert "已自动派发" not in data["advice"]
    assert not any("fields" in s or "crop_plantings" in s for s in diagnosis_db.info["statements"])
    await assert_no_records(diagnosis_db)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["未能获取合法图片文件", "图片解析失败", "未提供病叶图像",
                                     "非真实植物叶片照片/无法辨识"])
async def test_rejected_image_never_creates_fake_disease(diagnosis_client, diagnosis_db,
        diagnosis_users, diagnosis_fields, diagnosis_tools, refusal):
    vision, knowledge = diagnosis_tools
    vision.return_value.update(suspected_disease=refusal, confidence=0.0)
    response = await diagnosis_client.post("/api/diagnosis/visual-diagnose", json={
        "image_url": diagnosis_users["owner"].fixture_image_url, "field_id": diagnosis_fields["owner"][0].id,
    }, headers=headers(diagnosis_users["owner"]))
    assert response.status_code == 200
    data = response.json()
    assert data["disease_record_id"] is None
    assert data["ticket_id"] is None
    assert "未创建专家复核工单" in data["advice"]
    knowledge.assert_not_awaited()
    await assert_no_records(diagnosis_db)


@pytest.mark.asyncio
async def test_explicit_non_plant_features_block_even_high_confidence(diagnosis_client,
        diagnosis_db, diagnosis_users, diagnosis_fields, diagnosis_tools):
    vision, knowledge = diagnosis_tools
    vision.return_value.update(confidence=0.92, need_expert=True,
                              features_detected=["图像为人工绘制或简易示意图", "非真实植株叶片实拍照"])
    response = await diagnosis_client.post("/api/diagnosis/visual-diagnose", json={
        "image_url": diagnosis_users["owner"].fixture_image_url, "field_id": diagnosis_fields["owner"][0].id,
    }, headers=headers(diagnosis_users["owner"]))
    assert response.status_code == 200
    assert response.json()["disease_record_id"] is None
    assert response.json()["ticket_id"] is None
    knowledge.assert_not_awaited()
    await assert_no_records(diagnosis_db)


@pytest.mark.asyncio
async def test_real_uncertain_leaf_can_still_create_review_ticket(diagnosis_client, diagnosis_db,
        diagnosis_users, diagnosis_fields, diagnosis_tools):
    vision, _ = diagnosis_tools
    vision.return_value.update(suspected_disease="真实叶片病斑无法辨识", confidence=0.2,
                              features_detected=["真实叶脉", "叶绿素合成障碍疑似缺素"])
    response = await diagnosis_client.post("/api/diagnosis/visual-diagnose", json={
        "image_url": diagnosis_users["owner"].fixture_image_url, "field_id": diagnosis_fields["owner"][0].id,
    }, headers=headers(diagnosis_users["owner"]))
    assert response.status_code == 200
    data = response.json()
    assert data["need_expert"] is True
    assert data["ticket_id"] is not None
    record = await diagnosis_db.get(DiseaseRecord, data["ticket_id"])
    assert record.status == "pending_review"


@pytest.mark.asyncio
async def test_unauthenticated_api_does_not_run_diagnosis(diagnosis_client, diagnosis_db,
                                                      diagnosis_fields, diagnosis_users, diagnosis_tools):
    response = await diagnosis_client.post("/api/diagnosis/visual-diagnose", json={
        "image_url": diagnosis_users["owner"].fixture_image_url, "field_id": diagnosis_fields["owner"][0].id,
    })
    assert response.status_code in (401, 403)  # Existing HTTPBearer version policy.
    for tool in diagnosis_tools:
        tool.assert_not_awaited()
    await assert_no_records(diagnosis_db)


@pytest.mark.asyncio
async def test_historical_cs_cannot_diagnose_other_farm(diagnosis_client, diagnosis_db,
        diagnosis_users, diagnosis_fields, diagnosis_tools):
    response = await diagnosis_client.post("/api/diagnosis/visual-diagnose", json={
        "image_url": diagnosis_users["owner"].fixture_image_url, "field_id": diagnosis_fields["owner"][0].id,
    }, headers=headers(diagnosis_users["cs"]))
    assert response.status_code == 403
    for tool in diagnosis_tools: tool.assert_not_awaited()
    await assert_no_records(diagnosis_db)
