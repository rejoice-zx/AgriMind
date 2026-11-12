"""Synthetic regression checks for the October 14 audit fixes."""
from datetime import timedelta
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
import pytest_asyncio
from app.models.user import User
from app.models.farm import Farm
from app.core.weather_policy import agriculture_today
from app.core.pesticide_standards import find_all_banned_pesticides
from app.core.knowledge_contract import validate_effective_dates, source_quarantined
from app.models.crop import CropPlanting
from app.models.field import Field
from app.models.farming_record import FarmingRecord
from app.schemas.agriculture import FarmingRecordCreate, FarmingRecordCreateRequest, CropPlantingCreate, CropPlantingUpdate
from app.services.farm_service import FarmService
from app.services.farming_record_service import FarmingRecordService
from app.services.harvest_safety_service import harvest_safety


@pytest_asyncio.fixture
async def field_context(test_session):
    user = User(username='remediation-grower', password_hash='synthetic', role='customer', is_active=True)
    test_session.add(user); await test_session.flush()
    farm = Farm(name='synthetic farm', owner_id=user.id)
    test_session.add(farm); await test_session.flush()
    field = Field(name='synthetic field', farm_id=farm.id)
    test_session.add(field); await test_session.flush()
    crop = CropPlanting(field_id=field.id, crop='番茄', planting_date=agriculture_today(), status='active')
    test_session.add(crop); await test_session.commit()
    return user, farm, field, crop


@pytest.mark.parametrize('name', ['乙酰甲胺磷', '丁硫克百威', '乐果'])
def test_restricted_names_and_mixtures_are_not_masked(name):
    assert name in find_all_banned_pesticides('番茄拟用' + name + '复配制剂')
    assert name in find_all_banned_pesticides('禁止使用' + name)  # detection is not a recommendation


def test_unverified_phi_reference_table_was_removed():
    import app.core.pesticide_standards as standards
    # PHI comes only from the product label recorded by the farmer, never from a built-in table.
    assert not hasattr(standards, 'PESTICIDE_PHI_STANDARDS') and not hasattr(standards, 'lookup_pesticide_standard')


@pytest.mark.asyncio
@pytest.mark.parametrize('level', ['farm', 'field', 'crop_planting'])
async def test_public_deletion_cannot_destroy_history(test_session, field_context, level):
    user, farm, field, crop = field_context
    operation = getattr(FarmService, 'delete_' + level)
    key = {'farm': farm.id, 'field': field.id, 'crop_planting': crop.id}[level]
    with pytest.raises(HTTPException) as error:
        await operation(test_session, key, user.id)
    assert error.value.status_code == 409
    assert await test_session.get(CropPlanting, crop.id) is not None


@pytest.mark.asyncio
async def test_cross_field_batch_rejected_before_write(test_session, field_context):
    user, farm, field, crop = field_context
    other = Field(name='other synthetic', farm_id=farm.id)
    test_session.add(other); await test_session.flush()
    foreign = CropPlanting(field_id=other.id, crop='番茄', planting_date=agriculture_today())
    test_session.add(foreign); await test_session.flush()
    with pytest.raises(HTTPException) as error:
        await FarmingRecordService.create_farming_record(test_session, field.id,
            FarmingRecordCreate(record_type='施药', record_date=agriculture_today(), crop_planting_id=foreign.id), user.id)
    assert error.value.status_code == 422
    assert (await test_session.scalars(select(FarmingRecord))).all() == []


@pytest.mark.asyncio
async def test_unknown_phi_remains_unknown_and_actual_unsafe_fact_is_preserved(test_session, field_context):
    user, _, field, crop = field_context
    row = await FarmingRecordService.create_farming_record(test_session, field.id,
        FarmingRecordCreate(record_type='施药', record_date=agriculture_today(),
            crop_planting_id=crop.id, input_name='丁硫克百威'), user.id)
    assert row.safety_interval_days is None
    result = await harvest_safety(test_session, field.id, crop.id)
    assert result['status'] == 'blocked' and not result['can_approve']
    assert result['records'][0]['banned_names'] == ['丁硫克百威']


@pytest.mark.asyncio
async def test_phi_checks_all_records_not_only_last_five(test_session, field_context):
    user, _, field, crop = field_context
    crop.expected_harvest_date = agriculture_today() + timedelta(days=1)
    test_session.add(FarmingRecord(field_id=field.id, crop_planting_id=crop.id,
        record_type='施药', record_date=agriculture_today()-timedelta(days=2), safety_interval_days=15))
    for i in range(8):
        test_session.add(FarmingRecord(field_id=field.id, crop_planting_id=crop.id,
            record_type='inspection', record_date=agriculture_today()))
    await test_session.commit()
    result = await harvest_safety(test_session, field.id, crop.id)
    assert result['earliest_from_recorded_intervals'] == (agriculture_today()+timedelta(days=13)).isoformat()
    assert result['status'] == 'blocked'


def test_invalid_dates_never_silently_become_today():
    with pytest.raises(ValidationError):
        FarmingRecordCreateRequest(field_id=1, operation_date='2026-02-30')
    with pytest.raises(ValidationError):
        CropPlantingCreate(crop='番茄', planting_date='2026-10-14', expected_harvest_date='2026-10-13')
    with pytest.raises(ValidationError):
        CropPlantingUpdate(status='not-a-state', area=-2)
    with pytest.raises(HTTPException):
        validate_effective_dates('2026-01-01', '至今')


def test_seed_dates_valid_but_unreviewed_sources_quarantined():
    from app.services.seed_agriculture_knowledge import TOMATO_KNOWLEDGE_SEED
    for item in TOMATO_KNOWLEDGE_SEED:
        validate_effective_dates(item.get('effective_from'), item.get('effective_to'))
        assert source_quarantined(SimpleNamespace(**item))


@pytest.mark.asyncio
async def test_destructive_migration_rejected_before_init(monkeypatch):
    from app.migrations import migrate_json_to_sqlite as module
    async def forbidden(): raise AssertionError('must not initialize or open DB')
    monkeypatch.setattr(module, 'init_db', forbidden)
    with pytest.raises(ValueError, match='停用'):
        await module.JSONToSQLiteMigrator().run_all_migrations(clear_existing=True)


@pytest.mark.parametrize('samples', [
    [{'t_max': 20, 't_min': 10}],
    [{'date': '2026-01-01', 't_max': 5, 't_min': 10}],
    [{'date': d, 't_max': 20, 't_min': 10} for d in ['2026-01-01', '2026-01-01']],
    [{'date': d, 't_max': 20, 't_min': 10} for d in ['2026-01-01', '2026-01-03']],
])
def test_gdd_rejects_incomplete_or_invalid_history(samples):
    from app.core.gdd import calculate_thermal_sum
    with pytest.raises(ValueError):
        calculate_thermal_sum(daily_temps=samples)


@pytest.mark.asyncio
async def test_sqlite_snapshot_includes_wal_and_restore_cannot_overwrite(tmp_path, monkeypatch):
    import sqlite3
    from app.services.backup_service import BackupService
    source = tmp_path / 'synthetic.db'
    monkeypatch.setattr(BackupService, 'DB_PATH', source)
    monkeypatch.setattr(BackupService, 'BACKUP_DIR', tmp_path / 'backups')
    with sqlite3.connect(source) as live:
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('CREATE TABLE synthetic (value INTEGER)')
        live.execute('INSERT INTO synthetic VALUES (42)'); live.commit()
        success, _, filename = await BackupService.create_backup('synthetic WAL check')
        assert success
        with sqlite3.connect(BackupService.BACKUP_DIR / filename) as snapshot:
            assert snapshot.execute('SELECT value FROM synthetic').fetchone()[0] == 42
        live.execute('UPDATE synthetic SET value=43'); live.commit()
        restored, message = await BackupService.restore_backup(filename)
        assert restored is False and '停用' in message
        assert live.execute('SELECT value FROM synthetic').fetchone()[0] == 43


@pytest.mark.asyncio
async def test_multiple_active_batches_require_explicit_selection(test_session, field_context):
    from app.services.agent_service import AgriTools
    from app.services.agri_plan_service import AgriPlanService
    _, _, field, crop = field_context
    test_session.add(CropPlanting(field_id=field.id, crop='黄瓜', planting_date=agriculture_today()))
    await test_session.commit()
    result = await AgriTools.get_field_info(test_session, field.id)
    assert not result['found'] and '多个' in result['message']
    with pytest.raises(HTTPException):
        await AgriPlanService._planting(test_session, field.id)
    assert (await AgriPlanService._planting(test_session, field.id, crop.id)).id == crop.id


@pytest.mark.asyncio
async def test_json_export_preserves_source_and_plan_fields(test_session):
    from app.models.knowledge import KnowledgeItem
    from app.services.backup_service import BackupService
    test_session.add(KnowledgeItem(id='synthetic-export', question='test', answer='test',
        source_name='synthetic-only', effective_from='2026-01-01', source_type='quarantined', crop='番茄'))
    await test_session.commit()
    ok, _, data = await BackupService.export_to_json(test_session)
    assert ok and 'agri_plan_snapshots' in data
    row = next(x for x in data['knowledge_items'] if x['id'] == 'synthetic-export')
    assert row['source_name'] == 'synthetic-only' and row['crop'] == '番茄'
    assert row['effective_from'] == '2026-01-01' and row['source_type'] == 'quarantined'


def test_metadata_normalization_preserves_crop_and_region_boundaries():
    from app.core.knowledge_contract import metadata_matches
    item = SimpleNamespace(crop='番茄', disease='番茄灰霉病', growth_stage='开花坐果期', region='山东省')
    assert metadata_matches(item, 'disease', '灰霉病', '番茄')
    assert metadata_matches(item, 'growth_stage', '开花期', '番茄')
    assert metadata_matches(item, 'region', '山东省潍坊市', '番茄')
    assert not metadata_matches(item, 'region', '河北省', '番茄')
    assert not metadata_matches(item, 'crop', '黄瓜', '番茄')
    assert source_quarantined(SimpleNamespace(source_name='GB/T 23391', crop='番茄'))


@pytest.mark.asyncio
async def test_readiness_reports_empty_or_unavailable_data(test_session):
    import json
    from app.main import readiness_check
    from app.database import Base
    from app.core.schema_revision import record_schema_revision
    connection = await test_session.connection()
    await connection.run_sync(lambda sync: record_schema_revision(sync, Base.metadata))
    response = await readiness_check(test_session)
    assert response.status_code == 503
    assert json.loads(response.body)['knowledge_candidates'] == 0
    class Unavailable:
        async def execute(self, *args): raise RuntimeError('synthetic unavailable')
    response = await readiness_check(Unavailable())
    assert response.status_code == 503
    assert 'synthetic unavailable' not in response.body.decode()
