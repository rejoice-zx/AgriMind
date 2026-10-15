"""Main archive, reasoned reopening and shared metadata flows, synthetic sync storage only."""
from datetime import date, timedelta
from uuid import uuid4
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, select, func, MetaData
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.database import Base
from app.models import User, Farm, Field, CropPlanting, CropStatusEvent, FarmingRecord, FieldEnvironmentObservation, KnowledgeItem
from app.services.farm_service import FarmService
from app.services.crop_history_service import CropHistoryService
from app.services.field_environment_service import FieldEnvironmentService
from app.services.knowledge_service import KnowledgeService
from app.services.rag_service import RAGService
from app.schemas.agriculture import CropPlantingUpdate
from app.schemas.crop_history import CropReopenRequest
from app.core.knowledge_contract import metadata_matches, metadata_predicates
from app.core.schema_revision import install_history_guards, record_schema_revision, guard_schema_revision, CURRENT_SCHEMA_REVISION
from app.utils.time import utcnow
from tests.test_farming_submission_smoke import AsyncAdapter, finish_immediate


@pytest.fixture
def synthetic():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        install_history_guards(connection)
        record_schema_revision(connection, Base.metadata)
        assert guard_schema_revision(connection) == CURRENT_SCHEMA_REVISION
    with Session(engine, expire_on_commit=False) as session:
        owner = User(username='synthetic-archive-owner', password_hash='synthetic', role='customer')
        other = User(username='synthetic-other-owner', password_hash='synthetic', role='customer')
        expert = User(username='synthetic-read-only-expert', password_hash='synthetic', role='expert')
        admin = User(username='synthetic-archive-admin', password_hash='synthetic', role='admin')
        session.add_all([owner, other, expert, admin]); session.flush()
        farm = Farm(owner_id=owner.id, name='synthetic archive')
        session.add(farm); session.flush()
        field = Field(farm_id=farm.id, name='synthetic field', facility_type='greenhouse')
        session.add(field); session.commit()
        yield session, AsyncAdapter(session), owner, other, expert, admin, farm, field
    engine.dispose()


def test_metadata_sql_management_and_rag_share_candidates(synthetic, monkeypatch):
    session, db, *_ = synthetic
    defaults = dict(question='synthetic grey mould', answer='synthetic candidate, not an instruction', crop='番茄',
        region='山东省', growth_stage='开花坐果期', disease='番茄灰霉病', source_type='expert', authority_level='未核验')
    changes = {'normal': {}, 'all-stage': {'growth_stage': '全生育期', 'region': '全国'},
        'quarantined': {'source_type': 'quarantined'}, 'expired': {'effective_to': '2020-01-01'},
        'wrong-crop': {'crop': '黄瓜', 'disease': '黄瓜灰霉病'}, 'wrong-region': {'region': '河北省'},
        'wrong-disease': {'disease': '灰霉'}, 'ambiguous-region': {'region': '山'}}
    rows = [KnowledgeItem(id=key, **{**defaults, **change}) for key, change in changes.items()]
    session.add_all(rows); session.commit()
    filters = dict(crop='番茄', growth_stage='开花期', disease='灰霉病', region='山东潍坊市')
    python_ids = {row.id for row in rows if all(metadata_matches(row, key, value, filters['crop']) for key, value in filters.items())}
    sql_ids = set(session.scalars(select(KnowledgeItem.id).where(*metadata_predicates(KnowledgeItem, filters))))
    assert sql_ids == python_ids == {'normal', 'all-stage', 'quarantined', 'expired'}
    service = KnowledgeService.__new__(KnowledgeService)
    page1, total = finish_immediate(service.get_knowledge_list(db, page_size=2, **filters))
    page2, total2 = finish_immediate(service.get_knowledge_list(db, page=2, page_size=2, **filters))
    assert total == total2 == 4 and {row.id for row in [*page1, *page2]} == sql_ids
    assert set(finish_immediate(service.filter_candidate_ids(db, **filters))) == {'normal', 'all-stage'}
    rag = RAGService(use_rewrite=False)
    captured = []
    async def dense(queries, candidates, threshold):
        captured.extend(row['id'] for row in candidates)
        return [(row, 0.95) for row in candidates], {}
    monkeypatch.setattr(rag, '_vector_search_multi', dense)
    monkeypatch.setattr(rag, '_bm25_search', lambda query, candidates: {row['id']: 1.0 for row in candidates})
    result = finish_immediate(rag.hybrid_search('番茄灰霉病', rows, filter_metadata=filters, threshold=0.1, top_k=10))
    assert set(captured) == {'normal', 'all-stage'} and {row['id'] for row in result.retrieved_items} == {'normal', 'all-stage'}
    assert not metadata_matches({'region': '山东'}, 'region', '河北省')


def test_owner_and_admin_reopen_with_reason_once_and_keep_end_history(synthetic):
    session, db, owner, _, expert, admin, farm, field = synthetic
    crop = CropPlanting(field_id=field.id, crop='番茄', planting_date=date(2026, 9, 1), status='active')
    session.add(crop); session.commit()
    finish_immediate(FarmService.update_crop_planting(db, crop.id, CropPlantingUpdate(status='harvested'), owner.id))
    with pytest.raises(HTTPException) as bypass:
        finish_immediate(FarmService.update_crop_planting(db, crop.id, CropPlantingUpdate(status='active'), owner.id))
    assert bypass.value.status_code == 409
    with pytest.raises(ValidationError):
        CropReopenRequest(request_id=uuid4(), reason='   ')
    request = CropReopenRequest(request_id=uuid4(), reason='synthetic mistaken end')
    with pytest.raises(HTTPException) as forbidden:
        finish_immediate(CropHistoryService.reopen(db, crop.id, request, expert))
    assert forbidden.value.status_code == 403
    first = finish_immediate(CropHistoryService.reopen(db, crop.id, request, owner))
    replay = finish_immediate(CropHistoryService.reopen(db, crop.id, request, owner))
    assert crop.status == 'active' and first['event_id'] == replay['event_id'] and replay['replayed']
    with pytest.raises(HTTPException) as changed:
        finish_immediate(CropHistoryService.reopen(db, crop.id, request.model_copy(update={'reason': 'changed'}), owner))
    assert changed.value.status_code == 409
    events = finish_immediate(CropHistoryService.list_events(db, crop.id, owner))['items']
    assert [(row.previous_status, row.new_status) for row in events] == [('harvested', 'active'), ('active', 'harvested')]
    assert events[0].reason == request.reason
    another = CropPlanting(field_id=field.id, crop='黄瓜', planting_date=date(2026, 9, 1), status='terminated')
    session.add(another); session.commit()
    assert finish_immediate(CropHistoryService.reopen(db, another.id, CropReopenRequest(request_id=uuid4(), reason='synthetic admin correction'), admin))['crop'].status == 'active'
    with pytest.raises(IntegrityError, match='append only'):
        session.query(CropStatusEvent).filter_by(id=first['event_id']).update({'reason': 'overwritten'})
    session.rollback()
    farm.archived_at = utcnow(); session.commit()
    assert finish_immediate(CropHistoryService.reopen(db, crop.id, request, owner))['replayed']
    assert len(finish_immediate(CropHistoryService.list_events(db, crop.id, owner))['items']) == 2


def test_archive_pages_keep_a_snapshot_and_enforce_field_ownership(synthetic):
    session, db, owner, other, _, _, farm, field = synthetic
    crops = [CropPlanting(field_id=field.id, crop='番茄', planting_date=date(2026, 9, 1), status='harvested') for _ in range(5)]
    observations = [FieldEnvironmentObservation(field_id=field.id, request_id=str(uuid4()), request_fingerprint='synthetic',
        created_by_subject=owner.auth_subject, facility_type='greenhouse', measurement_scope='inside_facility',
        observed_at=utcnow() - timedelta(minutes=index+1), humidity=70+index) for index in range(5)]
    session.add_all([*crops, *observations]); session.commit()
    first = finish_immediate(CropHistoryService.list_crops(db, field.id, owner, limit=2))
    observed = finish_immediate(FieldEnvironmentService.history(db, field.id, owner, limit=2))
    session.add(CropPlanting(field_id=field.id, crop='new after snapshot', planting_date=date(2026, 10, 1)))
    session.add(FieldEnvironmentObservation(field_id=field.id, request_id=str(uuid4()), request_fingerprint='synthetic',
        created_by_subject=owner.auth_subject, facility_type='greenhouse', measurement_scope='inside_facility', observed_at=utcnow(), humidity=90))
    session.commit()
    remaining = finish_immediate(CropHistoryService.list_crops(db, field.id, owner, before_id=first['next_before_id'], snapshot_id=first['snapshot_id'], limit=20))
    older = finish_immediate(FieldEnvironmentService.history(db, field.id, owner, before_id=observed['next_before_id'], snapshot_id=observed['snapshot_id'], limit=20))
    assert {row.id for row in first['items'] + remaining['items']} == {crop.id for crop in crops}
    assert {row['id'] for row in observed['items'] + older['items']} == {row.id for row in observations}
    assert all('created_by_subject' not in row and 'request_fingerprint' not in row for row in older['items'])
    with pytest.raises(HTTPException) as forbidden:
        finish_immediate(FieldEnvironmentService.history(db, field.id, other))
    assert forbidden.value.status_code == 403


def test_batch_record_read_excludes_unbound_and_other_batches(synthetic):
    from app.api.farming_records import list_farming_records_compat
    session, db, owner, *_rest, field = synthetic
    crops = [CropPlanting(field_id=field.id, crop=name, planting_date=date(2026, 9, 1), status='harvested') for name in ('番茄', '黄瓜')]
    session.add_all(crops); session.flush()
    for crop_id in (crops[0].id, crops[1].id, None):
        session.add(FarmingRecord(field_id=field.id, crop_planting_id=crop_id, record_type='inspection', record_date=date(2026, 10, 1)))
    session.commit()
    result = finish_immediate(list_farming_records_compat(field.id, skip=0, limit=100, record_status='all', crop_planting_id=crops[0].id, current_user=owner, db=db))
    assert len(result) == 1 and result[0]['crop_planting_id'] == crops[0].id


def test_revision3_copy_adds_revision4_history_without_changing_old_records(monkeypatch):
    import app.core.schema_revision as revision
    engine = create_engine('sqlite:///:memory:')
    try:
        legacy = MetaData()
        for table in Base.metadata.sorted_tables:
            if table.name != 'crop_status_events':
                table.to_metadata(legacy)
        with engine.begin() as connection:
            legacy.create_all(connection)
            with monkeypatch.context() as old:
                old.setattr(revision, 'CURRENT_SCHEMA_REVISION', 3)
                install_history_guards(connection)
                record_schema_revision(connection, legacy)
            assert guard_schema_revision(connection) == 3
            connection.exec_driver_sql("INSERT INTO users(id,username,password_hash,role,auth_subject) VALUES(1,'synthetic-old','synthetic','customer','synthetic-old-subject')")
            connection.exec_driver_sql("INSERT INTO farms(id,name,owner_id) VALUES(1,'synthetic-old-farm',1)")
            connection.exec_driver_sql("INSERT INTO fields(id,name,farm_id) VALUES(1,'synthetic-old-field',1)")
            connection.exec_driver_sql("INSERT INTO crop_plantings(id,field_id,crop,planting_date,growth_stage,status) VALUES(1,1,'番茄','2026-09-01','采收期','harvested')")
            Base.metadata.create_all(connection)
            install_history_guards(connection)
            record_schema_revision(connection, Base.metadata, version=4)
            assert guard_schema_revision(connection) == 4
            assert connection.exec_driver_sql('SELECT status FROM crop_plantings WHERE id=1').scalar() == 'harvested'
            assert connection.exec_driver_sql('SELECT count(*) FROM crop_status_events').scalar() == 0
            assert connection.exec_driver_sql('SELECT count(*) FROM agri_schema_revisions').scalar() == 2
    finally:
        engine.dispose()
