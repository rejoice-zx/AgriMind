"""Signed consultation main flows using synthetic in-memory SQL only."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, update, func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.core.model_base import Base
from app.models import User, Farm, Field, CropPlanting, FarmingRecord, AgriConversation, AgriTurn, AgriConsultation, ConsultationReview
from app.schemas.agri_history import ConsultationAnswer
from app.services.agri_history_service import AgriHistoryService as History
from app.services.consultation_review_service import ConsultationReviewService as Reviews
from app.migrations.schema_upgrade_v6 import install_review_schema
from app.core.weather_policy import agriculture_today
from app.utils.time import utcnow
from tests.test_farming_submission_smoke import AsyncAdapter, finish_immediate as run


@pytest.fixture
def scene():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        Base.metadata.create_all(connection); install_review_schema(connection, Base.metadata)
    with Session(engine, expire_on_commit=False) as session:
        owner = User(username='synthetic-owner', password_hash='synthetic', role='customer')
        expert = User(username='synthetic-expert', display_name='synthetic signer', password_hash='synthetic', role='expert')
        admin = User(username='synthetic-admin', password_hash='synthetic', role='admin')
        session.add_all([owner, expert, admin]); session.flush()
        farm = Farm(owner_id=owner.id, name='synthetic'); session.add(farm); session.flush()
        field = Field(farm_id=farm.id, name='synthetic'); session.add(field); session.flush()
        crop = CropPlanting(field_id=field.id, crop='synthetic-crop', planting_date=agriculture_today()-timedelta(days=30))
        session.add(crop); session.flush()
        conversation = AgriConversation(owner_id=owner.id, field_id=field.id, field_ref=field.id, title='synthetic')
        session.add(conversation); session.flush()
        turn = AgriTurn(conversation_id=conversation.id, query='synthetic', request_id=str(uuid4()),
            request_payload={}, status='completed', decision={'query':'synthetic', 'conclusion':'synthetic',
                'decision_type':'HARVEST_DECISION', 'confidence':0.5, 'can_proceed':False, 'need_expert':True})
        session.add(turn); session.flush()
        ticket = AgriConsultation(turn_id=turn.id); session.add(ticket); session.commit()
        yield SimpleNamespace(s=session, db=AsyncAdapter(session), owner=owner, expert=expert, admin=admin,
            farm=farm, field=field, crop=crop, turn=turn, ticket=ticket)
    engine.dispose()


def approve(scene, purpose='harvest', products=None):
    context = run(Reviews.context(scene.db, scene.ticket.id, scene.expert, scene.crop.id))
    return ConsultationAnswer(answer='synthetic human review, not real agronomic evidence', request_id=uuid4(), operation_review={
        'purpose':purpose, 'outcome':'approved', 'crop_planting_id':scene.crop.id,
        'context_fingerprint':context['context_fingerprint'], 'planned_operation_date':agriculture_today(),
        'planned_harvest_date':agriculture_today()+timedelta(days=30), 'valid_until':utcnow()+timedelta(hours=1),
        'evidence_references':'synthetic source reference', 'review_notes':'synthetic site and complete history attestation',
        'checks':{key:True for key in ('registration_checked','crop_and_use_checked','records_complete','phi_checked','site_and_weather_checked')},
        'products':products or []})


def label(record, phi=3):
    return {'record_id':record.id, 'product_name':record.input_name, 'registration_number':'synthetic-registration',
        'applicable_crop':'synthetic-crop', 'label_reference':'synthetic original label reference',
        'usage_scope':'synthetic use and restrictions checked', 'phi_days':phi}


def test_account_lifetime_and_role_bind_assignment_and_signer(scene):
    run(History.claim(scene.db, scene.ticket.id, scene.expert))
    stale = SimpleNamespace(id=scene.expert.id, auth_subject=scene.expert.auth_subject, role='expert', is_active=True)
    scene.expert.auth_subject = str(uuid4()); scene.s.commit()
    with pytest.raises(HTTPException): run(History.resolve(scene.db, scene.ticket.id, stale, 'synthetic'))
    with pytest.raises(HTTPException) as inherited:
        run(History.resolve(scene.db, scene.ticket.id, scene.expert, 'synthetic'))
    assert inherited.value.status_code == 409
    run(History.claim(scene.db, scene.ticket.id, scene.admin))
    result = run(History.resolve(scene.db, scene.ticket.id, scene.admin, ConsultationAnswer(answer='synthetic admin', request_id=uuid4())))
    assert result.workflow_verified and result.reviewer_name == scene.admin.username
    with pytest.raises(HTTPException): run(History.claim(scene.db, scene.ticket.id, scene.owner))


def test_signed_receipt_replays_exact_body_and_is_immutable(scene):
    run(History.claim(scene.db, scene.ticket.id, scene.expert))
    body = approve(scene)
    first = run(History.resolve(scene.db, scene.ticket.id, scene.expert, body))
    replay = run(History.resolve(scene.db, scene.ticket.id, scene.expert, body))
    assert first.request_id == replay.request_id == str(body.request_id)
    assert first.reviewer_name == 'synthetic signer'
    assert scene.s.scalar(select(func.count()).select_from(ConsultationReview)) == 1
    assert scene.turn.decision['can_proceed'] is False
    assert scene.s.scalar(select(func.count()).select_from(FarmingRecord)) == 0
    assert run(Reviews.context(scene.db, scene.ticket.id, scene.owner))['current_review_status'] == 'current'
    with pytest.raises(HTTPException):
        run(History.resolve(scene.db, scene.ticket.id, scene.expert, body.model_copy(update={'answer':'changed'})))
    for statement in (update(ConsultationReview).values(resolved_name='changed'), update(AgriConsultation).values(answer='changed')):
        with pytest.raises(IntegrityError): scene.s.execute(statement)
        scene.s.rollback()
    scene.s.add(FarmingRecord(field_id=scene.field.id, crop_planting_id=scene.crop.id, record_type='irrigation', record_date=agriculture_today()))
    scene.s.commit()
    assert run(Reviews.context(scene.db, scene.ticket.id, scene.owner))['current_review_status'] == 'context_changed'


def test_incomplete_evidence_and_changed_context_cannot_approve(scene):
    run(History.claim(scene.db, scene.ticket.id, scene.expert))
    body = approve(scene)
    incomplete = body.model_copy(deep=True); incomplete.operation_review.checks.records_complete = False
    with pytest.raises(HTTPException) as failure: run(History.resolve(scene.db, scene.ticket.id, scene.expert, incomplete))
    assert failure.value.status_code == 422
    scene.s.add(FarmingRecord(field_id=scene.field.id, record_type='spraying', record_date=agriculture_today(), input_name='synthetic-product'))
    scene.s.commit()
    with pytest.raises(HTTPException) as stale: run(History.resolve(scene.db, scene.ticket.id, scene.expert, body))
    assert stale.value.status_code == 409
    assert scene.s.get(AgriConsultation, scene.ticket.id).status == 'in_review'
    assert scene.s.get(ConsultationReview, scene.ticket.id).resolved_payload is None


def test_harvest_covers_unbound_history_and_larger_phi(scene):
    for bound, days, phi in ((scene.crop.id, 10, 3), (None, 2, 7)):
        scene.s.add(FarmingRecord(field_id=scene.field.id, crop_planting_id=bound, record_type='病虫害防治',
            record_date=agriculture_today()-timedelta(days=days), input_name=f'synthetic-product-{days}', safety_interval_days=phi))
    scene.s.commit()
    records = list(scene.s.scalars(select(FarmingRecord).order_by(FarmingRecord.id)))
    run(History.claim(scene.db, scene.ticket.id, scene.expert))
    with pytest.raises(HTTPException): run(History.resolve(scene.db, scene.ticket.id, scene.expert, approve(scene, products=[label(records[0])])))
    with pytest.raises(HTTPException) as early:
        run(History.resolve(scene.db, scene.ticket.id, scene.expert, approve(scene, products=[label(row, 1) for row in records])))
    assert early.value.status_code == 422 and '间隔' in early.value.detail
    records[1].record_date = agriculture_today()-timedelta(days=8); scene.s.commit()
    result = run(History.resolve(scene.db, scene.ticket.id, scene.expert, approve(scene, products=[label(row, 1) for row in records])))
    assert result.signed_review.outcome == 'approved'


def test_old_text_answer_never_becomes_signed_approval(scene):
    scene.ticket.status = 'resolved'; scene.ticket.answer = 'synthetic historical answer'; scene.s.commit()
    item = History.consultation_item(scene.ticket, scene.turn, scene.s.get(AgriConversation, scene.turn.conversation_id), viewer=scene.expert)
    assert not item.workflow_verified and item.signed_review is None and not item.claim_available
    with pytest.raises(HTTPException): run(History.resolve(scene.db, scene.ticket.id, scene.expert, 'synthetic historical answer'))
