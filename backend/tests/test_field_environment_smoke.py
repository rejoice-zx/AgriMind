"""Main field-environment flow in synthetic storage; no model, provider or live database."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from pydantic import ValidationError
from fastapi import HTTPException
from sqlalchemy import create_engine, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.database import Base
from app.models import User, Farm, Field, FieldEnvironmentObservation
from app.schemas.field_environment import EnvironmentObservationCreate
from app.schemas.agriculture import FieldUpdate
from app.services.field_environment_service import FieldEnvironmentService
from app.core.field_environment import environment_context, bind_environment_weather
from app.core.schema_revision import install_history_guards, record_schema_revision, guard_schema_revision, CURRENT_SCHEMA_REVISION
from app.migrations.migrate_field_environment import migrate_field_environment
from app.utils.time import utcnow
from tests.test_farming_submission_smoke import AsyncAdapter, finish_immediate


def test_manual_observation_retry_archive_and_immutable_history():
    engine = create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            install_history_guards(connection)
            record_schema_revision(connection, Base.metadata)
            assert guard_schema_revision(connection) == CURRENT_SCHEMA_REVISION
        with Session(engine, expire_on_commit=False) as session:
            user = User(username='synthetic-environment-owner', password_hash='synthetic', role='customer')
            session.add(user); session.flush()
            farm = Farm(owner_id=user.id, name='synthetic')
            session.add(farm); session.flush()
            field = Field(farm_id=farm.id, name='synthetic', facility_type='greenhouse')
            session.add(field); session.commit()
            adapter = AsyncAdapter(session)
            payload = EnvironmentObservationCreate(request_id=uuid4(), facility_type='greenhouse',
                measurement_scope='inside_facility', observed_at=utcnow() - timedelta(minutes=5),
                temperature=28, humidity=82, ventilation='partial', covering='covered')
            first = finish_immediate(FieldEnvironmentService.create(adapter, field.id, payload, user))
            second = finish_immediate(FieldEnvironmentService.create(adapter, field.id, payload, user))
            assert first['observation_id'] == second['observation_id'] and second['replayed']
            assert second['environment']['observation_status'] == 'recent'
            assert not second['environment']['can_approve_operation']
            assert session.scalar(select(func.count()).select_from(FieldEnvironmentObservation)) == 1
            with pytest.raises(HTTPException) as changed:
                finish_immediate(FieldEnvironmentService.create(adapter, field.id, payload.model_copy(update={'humidity': 60}), user))
            assert changed.value.status_code == 409
            field.facility_type = 'open_field'; session.commit()
            assert finish_immediate(FieldEnvironmentService.latest(adapter, field))['observation_status'] == 'context_changed'
            with pytest.raises(HTTPException) as profile_changed:
                finish_immediate(FieldEnvironmentService.create(adapter, field.id, payload.model_copy(update={'request_id': uuid4()}), user))
            assert profile_changed.value.status_code == 409
            farm.archived_at = utcnow(); session.commit()
            assert finish_immediate(FieldEnvironmentService.create(adapter, field.id, payload, user))['replayed']
            with pytest.raises(HTTPException) as archived:
                finish_immediate(FieldEnvironmentService.create(adapter, field.id, payload.model_copy(update={'request_id': uuid4()}), user))
            assert archived.value.status_code == 409
            with pytest.raises(IntegrityError, match='append only'):
                session.query(FieldEnvironmentObservation).filter_by(id=first['observation_id']).update({'humidity': 1})
            session.rollback()
    finally:
        engine.dispose()


def test_weather_context_is_field_specific_and_old_observations_never_approve():
    now = utcnow()
    observation = SimpleNamespace(id=1, facility_type='greenhouse', measurement_scope='inside_facility',
        observed_at=now - timedelta(minutes=61), created_at=now, temperature=28, humidity=82,
        ventilation='open', covering='covered', notes=None)
    context = environment_context(SimpleNamespace(facility_type='greenhouse'), observation, now)
    assert context['observation_status'] == 'stale'
    provider_cache = {'spray_safety': {'is_suitable_to_spray': True, 'spray_risks': []}}
    covered = bind_environment_weather(provider_cache, context)
    outdoor = bind_environment_weather(provider_cache, environment_context(SimpleNamespace(facility_type='open_field')))
    unknown = bind_environment_weather(provider_cache, environment_context(SimpleNamespace(facility_type='unknown')))
    assert not covered['spray_safety']['is_suitable_to_spray']
    assert not unknown['spray_safety']['is_suitable_to_spray']
    assert outdoor['spray_safety']['is_suitable_to_spray']  # Weather condition only; no pesticide approval.
    assert provider_cache == {'spray_safety': {'is_suitable_to_spray': True, 'spray_risks': []}}
    outdoor['spray_safety']['spray_risks'].append('synthetic field-specific note')
    assert provider_cache['spray_safety']['spray_risks'] == []
    windy = {'spray_safety': {'is_suitable_to_spray': False, 'spray_risks': ['日最大风速10m/s ≥ 4m/s'], 'strong_wind_risk': True}}
    indoor = bind_environment_weather(windy, context)
    assert not indoor['spray_safety']['strong_wind_risk']
    assert all('≥' not in reason for reason in indoor['spray_safety']['spray_risks'])
    assert indoor['outdoor_weather_assessment']['strong_wind_risk']
    observation.observed_at = now - timedelta(minutes=1)
    observation.measurement_scope = 'onsite_outdoor'
    assert environment_context(SimpleNamespace(facility_type='greenhouse'), observation, now)['observation_status'] == 'scope_mismatch'


def test_weather_service_binds_context_after_shared_provider_cache(monkeypatch):
    from app.services.weather_service import WeatherService
    from app.services.settings_service import SettingsService
    provider_cache = {'status': 'online', 'is_fallback': False, 'summary': 'synthetic',
                      'spray_safety': {'is_suitable_to_spray': True, 'spray_risks': []}}
    async def config(_db):
        return SimpleNamespace(weather_provider='open_meteo')
    async def provider(**kwargs):
        return provider_cache
    monkeypatch.setattr(SettingsService, 'get_agri_ai_config', config)
    monkeypatch.setattr(WeatherService, 'get_real_weather', provider)
    engine = create_engine('sqlite:///:memory:')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            user = User(username='synthetic-weather-context', password_hash='synthetic', role='customer')
            session.add(user); session.flush()
            farm = Farm(owner_id=user.id, name='synthetic', latitude=30, longitude=110)
            session.add(farm); session.flush()
            covered = Field(farm_id=farm.id, name='covered', facility_type='greenhouse')
            outdoor = Field(farm_id=farm.id, name='outdoor', facility_type='open_field')
            session.add_all([covered, outdoor]); session.commit()
            bound = finish_immediate(WeatherService.get_field_weather(AsyncAdapter(session), covered))
            plain = finish_immediate(WeatherService.get_field_weather(AsyncAdapter(session), outdoor))
            assert bound['environment']['facility_type'] == 'greenhouse'
            assert not bound['spray_safety']['is_suitable_to_spray']
            assert plain['spray_safety']['is_suitable_to_spray']
            assert 'environment' not in provider_cache
            assert provider_cache['spray_safety']['spray_risks'] == []
    finally:
        engine.dispose()


def test_observation_input_and_legacy_upgrade_keep_unknown_as_unknown():
    body = dict(request_id=uuid4(), facility_type='unknown', measurement_scope='inside_facility', observed_at=utcnow(), humidity=70)
    for changes in ({'observed_at': utcnow() + timedelta(days=1)}, {'observed_at': '2026-10-14T12:00:00'},
                    {'humidity': float('nan')}, {'humidity': None}):
        with pytest.raises(ValidationError):
            EnvironmentObservationCreate(**{**body, **changes})
    with pytest.raises(ValidationError):
        FieldUpdate(facility_type=None)
    engine = create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE fields(id INTEGER PRIMARY KEY, name TEXT)')
            connection.exec_driver_sql("INSERT INTO fields(id,name) VALUES(1,'日光大棚')")
            migrate_field_environment(connection)
            migrate_field_environment(connection)
            assert connection.exec_driver_sql('SELECT facility_type FROM fields').scalar() == 'unknown'
    finally:
        engine.dispose()


def test_explicit_plan_batch_can_be_confirmed_when_two_batches_are_active(monkeypatch):
    from datetime import date
    from app.models import CropPlanting, FarmingRecord
    from app.schemas.agriculture import PlanItem
    from app.services.agri_plan_service import AgriPlanService
    from app.services.plan_confirmation import issue_confirmation
    from app.services.weather_service import WeatherService
    from app.core.weather_policy import agriculture_today
    async def weather(*args, **kwargs):
        return {'environment': environment_context(SimpleNamespace(facility_type='greenhouse')),
                'forecasts': [{'date': agriculture_today().isoformat(), 'rainfall_mm': 100, 'rain_prob': 100, 'temp_max': 50, 'wind_speed': 20}]}
    monkeypatch.setattr(WeatherService, 'get_field_weather', weather)
    engine = create_engine('sqlite:///:memory:')
    try:
        Base.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as session:
            user = User(username='synthetic-plan-owner', password_hash='synthetic', role='customer')
            session.add(user); session.flush()
            farm = Farm(owner_id=user.id, name='synthetic')
            session.add(farm); session.flush()
            field = Field(farm_id=farm.id, name='synthetic', facility_type='greenhouse')
            session.add(field); session.flush()
            crops = [CropPlanting(field_id=field.id, crop=name, planting_date=date(2026, 9, 1), status='active') for name in ('番茄', '黄瓜')]
            session.add_all(crops); session.commit()
            item = PlanItem(date=agriculture_today().isoformat(), record_type='field_management', action='核查现场',
                description='synthetic inspection', reason='synthetic', source='synthetic', crop_planting_id=crops[1].id)
            item.proof = issue_confirmation(item, field.id, user.id)
            result = finish_immediate(AgriPlanService.apply_plan_items(AsyncAdapter(session), field.id, [item], 'synthetic observer', user.id, user.role))
            record = session.get(FarmingRecord, result.record_ids[0])
            assert record.crop_planting_id == crops[1].id and record.record_type == 'inspection'
            assert '设施内' in record.attachments['weather_evidence']['environment']['notice']
            plan = AgriPlanService._build_plan(field, crops[1], finish_immediate(weather()), agriculture_today(), agriculture_today(), {})
            assert all('温室/大棚' in task.reason for task in plan.items)
            assert all('≥' not in task.reason for task in plan.items)
    finally:
        engine.dispose()
