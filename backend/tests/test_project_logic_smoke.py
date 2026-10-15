"""Student-project logic checks; synthetic data, no production scene or network."""
from datetime import timedelta
from uuid import uuid4
import pytest
from pydantic import ValidationError
from app.models import FieldEnvironmentObservation, WeatherSnapshot
from app.schemas.agriculture import (FarmCreate, FarmUpdate, FarmResponse, FieldCreate, FieldUpdate,
    FieldResponse, CropPlantingCreate, CropPlantingUpdate, CropPlantingResponse)
from app.services.farm_service import FarmService
from app.services.field_environment_service import FieldEnvironmentService
from app.services.consultation_review_service import ConsultationReviewService
from app.utils.time import utcnow
from tests.test_consultation_review_smoke import scene
from tests.test_farming_submission_smoke import finish_immediate as run


def test_invalid_asset_edits_are_rejected_before_invalid_data_can_be_persisted():
    for schema, payload in (
        (FarmUpdate, {'name': None}), (FieldUpdate, {'area': None}),
        (FarmCreate, {'name': '   '}), (FieldCreate, {'name': '\t'}),
        (FarmUpdate, {'area': float('inf')}), (FieldUpdate, {'area': float('nan')}),
        (FarmUpdate, {'province': 'x' * 51}), (FieldUpdate, {'soil_type': 'x' * 51}),
        (CropPlantingUpdate, {'crop': 'x' * 51}), (CropPlantingUpdate, {'growth_stage': '  '}),
        (CropPlantingUpdate, {'crop': None}), (CropPlantingCreate, {'crop': '  ', 'planting_date': '2026-10-14'}),
    ):
        with pytest.raises(ValidationError): schema.model_validate(payload)
    assert FarmUpdate().model_dump(exclude_unset=True) == {}
    assert FieldUpdate().model_dump(exclude_unset=True) == {}
    assert CropPlantingUpdate().model_dump(exclude_unset=True) == {}


def test_valid_partial_edits_preserve_response_contract_and_optional_clearing(scene):
    farm = run(FarmService.update_farm(scene.db, scene.farm.id, FarmUpdate(name='  synthetic renamed  ', area=0, description=None), scene.owner.id))
    assert FarmResponse.model_validate(farm).name == 'synthetic renamed'
    assert farm.area == 0 and farm.description is None
    field = run(FarmService.update_field(scene.db, scene.field.id, FieldUpdate(area=0, soil_type=None), scene.owner.id))
    assert FieldResponse.model_validate(field).area == 0 and field.soil_type is None
    crop = run(FarmService.update_crop_planting(scene.db, scene.crop.id, CropPlantingUpdate(variety=None, expected_harvest_date=None), scene.owner.id))
    assert CropPlantingResponse.model_validate(crop).variety is None
    assert crop.crop == 'synthetic-crop' and crop.planting_date == scene.crop.planting_date


def test_review_and_environment_agree_on_latest_observation_after_backfill(scene):
    now = utcnow()
    for age, temperature in ((2, 21), (60, 8), (2, 22)):
        scene.s.add(FieldEnvironmentObservation(field_id=scene.field.id, request_id=str(uuid4()),
            request_fingerprint='synthetic', created_by_subject=scene.owner.auth_subject,
            facility_type='unknown', measurement_scope='onsite_outdoor', observed_at=now-timedelta(minutes=age), temperature=temperature))
        scene.s.add(WeatherSnapshot(field_id=scene.field.id, timestamp=now-timedelta(minutes=age), temperature=temperature))
    scene.s.commit()
    context = run(ConsultationReviewService.context(scene.db, scene.ticket.id, scene.owner, scene.crop.id))
    latest = run(FieldEnvironmentService.latest(scene.db, scene.field))
    assert context['snapshot']['latest_environment']['temperature'] == 22
    assert context['snapshot']['latest_weather']['temperature'] == 22
    assert context['snapshot']['latest_environment']['id'] == latest['latest_observation']['id']
