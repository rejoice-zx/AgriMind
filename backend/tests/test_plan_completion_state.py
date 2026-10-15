"""Completion display is derived from durable receipts, not browser flags."""
import pytest
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.api.agri_plan import router
from app.api.dependencies import get_current_user
from app.database import get_db
from app.models.crop import CropPlanting
from app.models.field import Field
from app.models.farming_record import FarmingRecord
from app.models.plan_execution import PlanExecution
from app.schemas.agriculture import AgriPlanRequest
from app.services.agri_plan_service import AgriPlanService
from app.services.farming_record_service import FarmingRecordService
from app.core.weather_policy import agriculture_today
from tests.test_agri_optimization_a import field_context, actual_weather


@pytest.mark.asyncio
async def test_confirmation_survives_fresh_session_and_regenerated_plan(test_session, field_context, actual_weather):
    user, _, field, _ = field_context
    plan = await AgriPlanService.generate_plan(test_session, AgriPlanRequest(field_id=field.id), user.id, user.role)
    assert plan.items[0].execution_status == 'pending'
    applied = await AgriPlanService.apply_plan_items(test_session, field.id, [plan.items[0]], 'test', user.id, user.role)
    # A new ORM session/app has no in-memory completion flags to rely on.
    sessions = async_sessionmaker(test_session.bind, expire_on_commit=False)
    app = FastAPI(); app.include_router(router)
    async def database():
        async with sessions() as session:
            yield session
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        regenerated = await client.post('/api/agri/plan', json={'field_id': field.id, 'days': 14})
        assert regenerated.status_code == 200
        item = regenerated.json()['items'][0]
        assert item['execution_status'] == 'completed'
        assert item['execution_record_id'] == applied.record_ids[0]
        assert all(other['execution_status'] == 'pending' for other in regenerated.json()['items'][1:])
        replay = await client.post('/api/agri/plan/apply', json={'field_id': field.id, 'items': [item]})
        assert replay.status_code == 200
        assert replay.json()['created_count'] == 0 and replay.json()['record_ids'] == applied.record_ids


@pytest.mark.asyncio
async def test_revoked_receipt_is_displayed_and_cannot_be_reconfirmed_by_changing_ui_flags(test_session, field_context, actual_weather):
    user, _, field, _ = field_context
    request = AgriPlanRequest(field_id=field.id)
    plan = await AgriPlanService.generate_plan(test_session, request, user.id, user.role)
    applied = await AgriPlanService.apply_plan_items(test_session, field.id, [plan.items[0]], 'test', user.id, user.role)
    await FarmingRecordService.delete_farming_record(test_session, applied.record_ids[0], user.id)
    refreshed = await AgriPlanService.generate_plan(test_session, request, user.id, user.role)
    assert refreshed.items[0].execution_status == 'revoked'
    assert refreshed.items[0].execution_record_id == applied.record_ids[0]
    forged_ui = refreshed.items[0].model_copy(update={'execution_status': 'pending', 'execution_record_id': None})
    with pytest.raises(HTTPException) as error:
        await AgriPlanService.apply_plan_items(test_session, field.id, [forged_ui], 'test', user.id, user.role)
    assert error.value.status_code == 409
    assert await test_session.scalar(select(func.count(FarmingRecord.id))) == 1


@pytest.mark.asyncio
async def test_missing_record_is_unavailable_and_not_a_new_pending_task(test_session, field_context, actual_weather):
    user, _, field, _ = field_context
    request = AgriPlanRequest(field_id=field.id)
    plan = await AgriPlanService.generate_plan(test_session, request, user.id, user.role)
    await AgriPlanService.apply_plan_items(test_session, field.id, [plan.items[0]], 'test', user.id, user.role)
    receipt = await test_session.scalar(select(PlanExecution))
    receipt.record_id = None
    await test_session.commit()
    refreshed = await AgriPlanService.generate_plan(test_session, request, user.id, user.role)
    assert refreshed.items[0].execution_status == 'unavailable'
    with pytest.raises(HTTPException):
        await AgriPlanService.apply_plan_items(test_session, field.id, [refreshed.items[0]], 'test', user.id, user.role)


@pytest.mark.asyncio
async def test_confirmation_is_scoped_to_field_and_planting(test_session, field_context, actual_weather):
    user, farm, field, planting = field_context
    plan = await AgriPlanService.generate_plan(test_session, AgriPlanRequest(field_id=field.id), user.id, user.role)
    await AgriPlanService.apply_plan_items(test_session, field.id, [plan.items[0]], 'test', user.id, user.role)
    second = Field(farm_id=farm.id, name='Another field', area=1)
    test_session.add(second); await test_session.flush()
    test_session.add(CropPlanting(field_id=second.id, crop='番茄', planting_date=agriculture_today(), status='active'))
    await test_session.commit()
    second_plan = await AgriPlanService.generate_plan(test_session, AgriPlanRequest(field_id=second.id), user.id, user.role)
    assert second_plan.items[0].execution_status == 'pending'
    planting.status = 'completed'
    test_session.add(CropPlanting(field_id=field.id, crop='黄瓜', planting_date=agriculture_today(), status='active'))
    await test_session.commit()
    next_planting_plan = await AgriPlanService.generate_plan(test_session, AgriPlanRequest(field_id=field.id), user.id, user.role)
    assert next_planting_plan.items[0].execution_status == 'pending'
