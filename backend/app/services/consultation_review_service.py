"""Human review receipts bound to an account lifetime and a complete local context.

No model/provider calls. Human source attestations are retained as attestations;
an approved receipt never modifies the original AI decision or records work.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select, update, text, or_
from app.models import (User, Farm, Field, CropPlanting, FarmingRecord, WeatherSnapshot,
    FieldEnvironmentObservation, AgriConsultation, ConsultationReview)
from app.models.farming_record import is_spraying_record
from app.schemas.agri_history import ConsultationAnswer
from app.core.roles import AGRI_VALUES, is_agri_staff
from app.core.submission import submission_fingerprint
from app.core.weather_policy import agriculture_today
from app.core.pesticide_standards import find_all_banned_pesticides
from app.utils.time import utcnow
from app.core.audit_logging import audit


def _serialize(row, excluded=()):
    return {column.name: value.isoformat() if isinstance(value, (date, datetime)) else value
        for column in row.__table__.columns if column.name not in excluded
        for value in [getattr(row, column.name)]}


class ConsultationReviewService:
    @staticmethod
    def current_assignment(ticket):
        person, review = ticket.assigned_expert, ticket.review
        return bool(person and review and person.is_active and is_agri_staff(person)
            and review.assigned_subject == person.auth_subject)

    @staticmethod
    def eligible(user):
        if not is_agri_staff(user) or not getattr(user, 'is_active', False):
            raise HTTPException(403, '仅有效的农技专家或管理员可签署复核')
        return select(User.id).where(User.id == user.id, User.auth_subject == user.auth_subject,
            User.is_active.is_(True), User.role.in_(AGRI_VALUES)).exists()

    @staticmethod
    async def ticket(db, ticket_id, user):
        from app.services.agri_history_service import AgriHistoryService
        return await AgriHistoryService.ticket(db, ticket_id, user)

    @classmethod
    async def claim(cls, db, ticket_id, user):
        eligible = cls.eligible(user)
        if not await db.scalar(select(eligible)): raise HTTPException(403, '账号状态已变化')
        ticket, *_ = await cls.ticket(db, ticket_id, user)
        if ticket.status == 'in_review' and ticket.assigned_expert_id == user.id and cls.current_assignment(ticket):
            return ticket
        assigned = select(ConsultationReview.consultation_id).join(AgriConsultation,
            AgriConsultation.id == ConsultationReview.consultation_id).join(User, User.id == AgriConsultation.assigned_expert_id).where(
                ConsultationReview.consultation_id == str(ticket_id), User.auth_subject == ConsultationReview.assigned_subject,
                User.is_active.is_(True), User.role.in_(AGRI_VALUES)).exists()
        signed = select(ConsultationReview.consultation_id).where(ConsultationReview.consultation_id == str(ticket_id),
            ConsultationReview.resolved_payload.is_not(None)).exists()
        changed = await db.execute(update(AgriConsultation).where(AgriConsultation.id == str(ticket_id),
            AgriConsultation.status.in_(['pending', 'in_review']), AgriConsultation.answer.is_(None), ~assigned, ~signed, eligible)
            .values(status='in_review', assigned_expert_id=user.id, updated_at=utcnow()))
        if changed.rowcount != 1: raise HTTPException(409, '咨询已有有效接单人或已答复，请刷新核对')
        review = await db.get(ConsultationReview, str(ticket_id), populate_existing=True)
        if review is None:
            review = ConsultationReview(consultation_id=str(ticket_id)); db.add(review)
        review.assigned_subject = user.auth_subject
        await db.commit()
        audit('consultation_claimed', consultation_id=str(ticket_id), actor_id=user.id)

    @classmethod
    async def context(cls, db, ticket_id, user, crop_id=None):
        ticket, turn, conversation = await cls.ticket(db, ticket_id, user)
        original_crop = (turn.request_payload or {}).get('crop_planting_id')
        signed = (ticket.review.resolved_payload or {}).get('operation_review') if ticket.review else None
        if original_crop is not None:
            if crop_id is not None and crop_id != original_crop: raise HTTPException(409, '不能将原提问复核改绑到另一批次')
            crop_id = original_crop
        elif crop_id is None and signed:
            crop_id = signed.get('crop_planting_id')
        snapshot = {'consultation_id': ticket.id, 'turn_id': turn.id, 'original_query': turn.query,
            'original_decision': turn.decision, 'field_id': conversation.field_ref,
            'field': None, 'farm': None, 'crop': None, 'records': [], 'latest_environment': None, 'latest_weather': None}
        candidates = []
        field = await db.get(Field, conversation.field_ref, populate_existing=True) if conversation.field_ref else None
        if field is not None and conversation.field_id == field.id:
            farm = await db.get(Farm, field.farm_id, populate_existing=True)
            snapshot['field'], snapshot['farm'] = _serialize(field), _serialize(farm) if farm else None
            crops = list((await db.scalars(select(CropPlanting).where(CropPlanting.field_id == field.id,
                CropPlanting.status == 'active').order_by(CropPlanting.id))).all())
            candidates = [{'id': row.id, 'crop': row.crop, 'planting_date': row.planting_date.isoformat()} for row in crops]
            if crop_id is not None:
                crop = await db.get(CropPlanting, crop_id, populate_existing=True)
                if crop is None or crop.field_id != field.id: raise HTTPException(422, '复核批次不存在或不属于原田块')
                snapshot['crop'] = _serialize(crop)
                records = list((await db.scalars(select(FarmingRecord).where(FarmingRecord.field_id == field.id,
                    or_(FarmingRecord.crop_planting_id == crop.id, (FarmingRecord.crop_planting_id.is_(None)) &
                        (FarmingRecord.record_date >= crop.planting_date))).order_by(FarmingRecord.id))).all())
                snapshot['records'] = [_serialize(row, {'created_by_subject', 'revoked_by_subject', 'request_fingerprint'}) for row in records]
            for model, key, observed in ((FieldEnvironmentObservation, 'latest_environment', FieldEnvironmentObservation.observed_at),
                    (WeatherSnapshot, 'latest_weather', WeatherSnapshot.timestamp)):
                row = await db.scalar(select(model).where(model.field_id == field.id).order_by(observed.desc(), model.id.desc()).limit(1))
                if row: snapshot[key] = _serialize(row, {'created_by_subject', 'request_fingerprint'})
        fingerprint = submission_fingerprint(conversation.field_ref, snapshot)
        status = 'no_signed_operation_review'
        if signed:
            status = 'historical_non_approval'
            if signed['outcome'] == 'approved':
                expiry = datetime.fromisoformat(signed['valid_until'])
                status = 'expired' if expiry <= utcnow() else 'context_changed' if signed['context_fingerprint'] != fingerprint else 'current'
        return {'context_fingerprint': fingerprint, 'snapshot': snapshot, 'available_crops': candidates,
            'spraying_record_ids': [row['id'] for row in snapshot['records'] if row['revoked_at'] is None and is_spraying_record(row['record_type'])],
            'current_review_status': status, 'review_window_hours': 24,
            'note': '24小时为应用复核有效窗口上限，不是农艺标准；快照不证明记录完整或来源真实，现场情况仍须人工核实。'}

    @staticmethod
    def validate_operation(review, context):
        if review.context_fingerprint != context['context_fingerprint']:
            raise HTTPException(409, '田块、批次、履历或环境资料已变化，请重新读取并核对复核上下文')
        if review.outcome != 'approved': return
        now, today = utcnow(), agriculture_today()
        snap, crop = context['snapshot'], context['snapshot']['crop']
        if (not crop or crop['status'] != 'active' or not snap['field'] or not snap['farm'] or
                snap['field']['archived_at'] is not None or snap['farm']['archived_at'] is not None):
            raise HTTPException(422, '仅原田块的明确在种批次且资产可写时可签署通过')
        if review.crop_planting_id != crop['id']:
            raise HTTPException(422, '复核须明确当前批次')
        if not all(review.checks.model_dump().values()) or not review.evidence_references.strip() or not review.review_notes.strip():
            raise HTTPException(422, '资料不足：须逐项核对登记/标签、作物用途、完整履历、PHI及现场天气，并填写依据和结论理由')
        if review.valid_until is None or not now < review.valid_until <= now + timedelta(hours=24):
            raise HTTPException(422, '通过复核须填写未来24小时内的明确截止时间；此为应用防陈旧策略')
        if review.planned_operation_date is None or not today <= review.planned_operation_date <= review.valid_until.astimezone(today_timezone()).date():
            raise HTTPException(422, '拟作业日期须在当前日期及复核截止日期范围内')
        if review.planned_operation_date < date.fromisoformat(crop['planting_date']):
            raise HTTPException(422, '拟作业日期不能早于本批次种植日期')
        for product in review.products:
            if product.applicable_crop.strip() != crop['crop'].strip(): raise HTTPException(422, '标签适用作物须与当前批次明确一致')
            if find_all_banned_pesticides(product.product_name): raise HTTPException(422, '产品命中本应用禁限用名单，不能签署通过；须另外核对最新目录及适用范围')
        active_sprays = [row for row in snap['records'] if row['revoked_at'] is None and is_spraying_record(row['record_type'])]
        for row in active_sprays:
            if find_all_banned_pesticides(row['input_name'] or ''): raise HTTPException(422, '历史施药命中本应用禁限用名单，不能签署通过')
        if review.purpose == 'spray':
            if not review.products or any(item.record_id is not None for item in review.products):
                raise HTTPException(422, '施药复核须填写拟用产品完整标签依据，不将历史记录冒充本次拟用产品')
            if review.planned_harvest_date is None or review.planned_harvest_date < review.planned_operation_date + timedelta(days=max(item.phi_days for item in review.products)):
                raise HTTPException(422, '须明确计划采收日期，并满足本次所填产品标签的最大PHI；系统不代填标签值')
            for row in active_sprays:
                if row['safety_interval_days'] is None:
                    raise HTTPException(422, '既往施药记录PHI未知，须先核实履历，不能通过新施药复核')
                if review.planned_harvest_date < date.fromisoformat(row['record_date']) + timedelta(days=row['safety_interval_days']):
                    raise HTTPException(422, '计划采收日期仍在既往施药安全间隔内，不能通过')
        else:
            products = {item.record_id: item for item in review.products}
            if len(products) != len(review.products) or set(products) != {row['id'] for row in active_sprays}:
                raise HTTPException(422, '采收复核须逐条覆盖本批次及定植后未绑定的有效施药记录，不能遗漏或重复引用')
            for row in active_sprays:
                item = products[row['id']]
                if item.product_name != (row['input_name'] or '').strip():
                    raise HTTPException(422, '历史产品名称与核验标签不一致或不完整，须先核实原始记录，不自动匹配简称')
                recorded_phi = row['safety_interval_days']
                phi = max(item.phi_days, recorded_phi if recorded_phi is not None else item.phi_days)
                if review.planned_operation_date < date.fromisoformat(row['record_date']) + timedelta(days=phi):
                    raise HTTPException(422, '拟采收日期仍在施药记录/核验标签的安全间隔内，不能通过')

    @classmethod
    async def resolve(cls, db, ticket_id, user, payload):
        payload = ConsultationAnswer(answer=payload) if isinstance(payload, str) else payload
        values = payload.model_dump(mode='json')
        actor = SimpleNamespace(id=user.id, auth_subject=user.auth_subject, role=user.role, is_active=user.is_active)
        cls.eligible(actor)
        await db.rollback()
        try:
            # No external calls inside this short SQLite write transaction. Context and signature are serialized with farming writes.
            await db.execute(text('BEGIN IMMEDIATE'))
            current = await db.scalar(select(User).where(User.id == actor.id, User.auth_subject == actor.auth_subject,
                User.is_active.is_(True), User.role.in_(AGRI_VALUES)).execution_options(populate_existing=True))
            if current is None: raise HTTPException(403, '账号已失效或权限已变化')
            ticket, turn, conversation = await cls.ticket(db, ticket_id, actor)
            receipt = ticket.review
            if receipt and receipt.resolved_payload:
                if receipt.resolved_subject == actor.auth_subject and receipt.resolved_payload == values:
                    await db.commit(); return
                raise HTTPException(409, '签署回执已保存；不能修改内容、换签署人或更换编号覆盖')
            if (ticket.status != 'in_review' or ticket.assigned_expert_id != actor.id or not receipt or
                    receipt.assigned_subject != actor.auth_subject):
                raise HTTPException(409, '请先有效接单；旧答复不能补成已签署批准')
            if payload.request_id:
                used = await db.scalar(select(ConsultationReview).where(ConsultationReview.request_id == str(payload.request_id)))
                if used: raise HTTPException(409, '提交编号已用于另一份复核，请核对原回执')
            context = None
            if payload.operation_review:
                context = await cls.context(db, ticket_id, actor, payload.operation_review.crop_planting_id)
                cls.validate_operation(payload.operation_review, context)
            changed = await db.execute(update(AgriConsultation).where(AgriConsultation.id == str(ticket_id),
                AgriConsultation.status == 'in_review', AgriConsultation.assigned_expert_id == actor.id,
                cls.eligible(actor)).values(status='resolved', answer=payload.answer, updated_at=utcnow()))
            if changed.rowcount != 1: raise HTTPException(409, '接单或账号状态已变化')
            receipt.request_id = str(payload.request_id) if payload.request_id else None
            receipt.resolved_payload = values
            receipt.context_snapshot = context['snapshot'] if context else None
            receipt.resolved_by_id, receipt.resolved_subject = actor.id, actor.auth_subject
            receipt.resolved_name = current.display_name or current.username
            receipt.resolved_at = utcnow()
            await db.commit()
        except BaseException:
            await db.rollback(); raise
        audit('consultation_signed', consultation_id=str(ticket_id), actor_id=actor.id)


def today_timezone():
    from app.core.weather_policy import AGRI_TIMEZONE
    return AGRI_TIMEZONE
