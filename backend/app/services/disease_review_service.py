"""Atomic assignments and immutable feedback; never commits through knowledge CRUD helpers."""
from datetime import datetime, timezone
from uuid import uuid4
from app.core.audit_logging import audit, bind_audit
from fastapi import HTTPException
from sqlalchemy import select, update, delete
from sqlalchemy.orm import selectinload
from app.models import DiseaseRecord, DiseaseReview, ExpertCase, KnowledgeItem, User
from app.core.roles import AGRI_VALUES, ADMIN_VALUES, is_agri_staff
from app.schemas.expert import ExpertResolveResponse, ExpertFeedback
from app.utils.time import utcnow


class DiseaseReviewService:
    @staticmethod
    async def delete_record(db, record_id, user):
        if user.role not in ADMIN_VALUES or not user.is_active:
            raise HTTPException(403, "仅有效的管理员可删除病害记录")
        eligible = select(User.id).where(User.id == user.id, User.auth_subject == user.auth_subject,
            User.is_active.is_(True), User.role.in_(ADMIN_VALUES)).exists()
        # FK cascades remove reviews/cases; request receipts retain their identity
        # with record_id SET NULL so old replies cannot resurrect a deleted case.
        # Knowledge entries and uploaded images have independent lifecycles.
        try:
            field_id = await db.scalar(delete(DiseaseRecord).where(DiseaseRecord.id == record_id, eligible)
                .returning(DiseaseRecord.field_id).execution_options(synchronize_session=False))
            if field_id is None:
                if not await db.scalar(select(eligible)):
                    raise HTTPException(403, "管理员账号已失效或权限已变化")
                raise HTTPException(404, "病害记录不存在或已删除")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        audit("disease_record_deleted", record_id=record_id, field_id=field_id, actor_id=user.id)

    @staticmethod
    def record_query():
        from app.models.field import Field
        return select(DiseaseRecord).options(
            selectinload(DiseaseRecord.field).selectinload(Field.farm),
            selectinload(DiseaseRecord.crop_planting),
            selectinload(DiseaseRecord.expert_cases).selectinload(ExpertCase.expert),
            selectinload(DiseaseRecord.review).selectinload(DiseaseReview.assigned_expert),
        ).execution_options(populate_existing=True)

    @staticmethod
    def eligible(user):
        if not is_agri_staff(user) or not getattr(user, "is_active", False):
            raise HTTPException(403, "仅有效的农技专家或管理员可认领与复核")
        return select(User.id).where(User.id == user.id, User.auth_subject == user.auth_subject,
            User.is_active.is_(True), User.role.in_(AGRI_VALUES)).exists()

    @staticmethod
    async def record(db, record_id):
        record = await db.scalar(DiseaseReviewService.record_query().where(DiseaseRecord.id == record_id))
        if record is None:
            raise HTTPException(404, "病害记录不存在")
        return record

    @staticmethod
    def current_assignment(record):
        review = record.review
        expert = review.assigned_expert if review else None
        return bool(expert and expert.is_active and is_agri_staff(expert) and expert.auth_subject == review.assigned_subject)

    @staticmethod
    def claim_available(record):
        return (record.status in {"pending_review", "in_review"} and not record.expert_cases
                and record.expert_result is None
                and not (record.review and record.review.resolved_payload)
                and not DiseaseReviewService.current_assignment(record))

    @staticmethod
    def feedback(record):
        review = record.review
        if review and review.resolved_payload:
            payload = review.resolved_payload
            return ExpertFeedback(confirmed_disease=payload["confirmed_disease"],
                treatment_suggestion=payload["treatment_suggestion"], notes=payload.get("notes"),
                expert_case_id=review.result_case_id, expert_name=review.resolved_expert_name,
                resolved_at=review.resolved_at, workflow_verified=True)
        if record.expert_cases:
            case = max(record.expert_cases, key=lambda item: (item.created_at or datetime.min.replace(tzinfo=timezone.utc), item.id))
            return ExpertFeedback(confirmed_disease=case.confirmed_disease, treatment_suggestion=case.treatment_suggestion,
                notes=case.notes, expert_case_id=case.id,
                expert_name=(case.expert.display_name or case.expert.username) if case.expert else None,
                resolved_at=case.created_at, workflow_verified=False)
        if record.expert_result:
            return ExpertFeedback(treatment_suggestion=record.expert_result, resolved_at=record.updated_at, workflow_verified=False)
        return None

    @staticmethod
    async def claim(db, record_id, user):
        eligible = DiseaseReviewService.eligible(user)
        if not await db.scalar(select(eligible)):
            raise HTTPException(403, "专家账号已失效或权限已变化")
        record = await DiseaseReviewService.record(db, record_id)
        bind_audit(record_id=record_id, field_id=record.field_id)
        review = record.review
        if (record.status == "in_review" and review and review.assigned_expert_id == user.id
                and review.assigned_subject == user.auth_subject and DiseaseReviewService.current_assignment(record)):
            return
        assigned = select(DiseaseReview.disease_record_id).join(User, User.id == DiseaseReview.assigned_expert_id).where(
            DiseaseReview.disease_record_id == record_id, User.auth_subject == DiseaseReview.assigned_subject,
            User.is_active.is_(True), User.role.in_(AGRI_VALUES)).exists()
        resolved = select(DiseaseReview.disease_record_id).where(DiseaseReview.disease_record_id == record_id,
            DiseaseReview.resolved_payload.is_not(None)).exists()
        cases = select(ExpertCase.id).where(ExpertCase.disease_record_id == record_id).exists()
        result = await db.execute(update(DiseaseRecord).where(DiseaseRecord.id == record_id,
            DiseaseRecord.status.in_(["pending_review", "in_review"]), DiseaseRecord.expert_result.is_(None), ~assigned, ~resolved, ~cases, eligible
        ).values(status="in_review", updated_at=utcnow()))
        if not result.rowcount:
            current = await DiseaseReviewService.record(db, record_id)
            if (current.status == "in_review" and current.review and current.review.assigned_expert_id == user.id
                    and current.review.assigned_subject == user.auth_subject and DiseaseReviewService.current_assignment(current)):
                return
            raise HTTPException(409, "工单已被认领、已复核或账号状态已变化，请刷新核对")
        review = await db.get(DiseaseReview, record_id)
        if review is None:
            review = DiseaseReview(disease_record_id=record_id)
            db.add(review)
        review.assigned_expert_id = user.id
        review.assigned_subject = user.auth_subject
        await db.commit()
        audit("disease_claimed", record_id=record_id, field_id=record.field_id, actor_id=user.id)

    @staticmethod
    async def request_review(db, record_id):
        record = await DiseaseReviewService.record(db, record_id)
        bind_audit(record_id=record_id, field_id=record.field_id)
        if record.status in {"pending_review", "in_review", "expert_reviewed"}:
            return
        if record.status != "preliminary" or record.expert_cases or record.expert_result:
            raise HTTPException(409, "记录已有结论或已关闭，不能重新派单")
        result = await db.execute(update(DiseaseRecord).where(DiseaseRecord.id == record_id,
            DiseaseRecord.status == "preliminary").values(status="pending_review", updated_at=utcnow()))
        if result.rowcount:
            await db.commit()
            audit("disease_review_requested", record_id=record_id, field_id=record.field_id)

    @staticmethod
    async def resolve(db, record_id, user, payload):
        eligible = DiseaseReviewService.eligible(user)
        if not await db.scalar(select(eligible)):
            raise HTTPException(403, "专家账号已失效或权限已变化")
        record = await DiseaseReviewService.record(db, record_id)
        bind_audit(record_id=record_id, field_id=record.field_id)
        review = record.review
        values = payload.model_dump(mode="json")
        if review and review.resolved_payload:
            if (review.assigned_expert_id == user.id and review.assigned_subject == user.auth_subject
                    and review.resolved_payload == values):
                case = await db.get(ExpertCase, review.result_case_id) if review.result_case_id else None
                if case is None: raise HTTPException(409, "原归档引用已缺失，请管理员核对；本次不重复生成")
                return DiseaseReviewService.response(review)
            raise HTTPException(409, "复核已保存，本次内容或责任人不同，不能覆盖")
        owned = select(DiseaseReview.disease_record_id).where(DiseaseReview.disease_record_id == record_id,
            DiseaseReview.assigned_expert_id == user.id, DiseaseReview.assigned_subject == user.auth_subject,
            DiseaseReview.resolved_payload.is_(None)).exists()
        cases = select(ExpertCase.id).where(ExpertCase.disease_record_id == record_id).exists()
        result = await db.execute(update(DiseaseRecord).where(DiseaseRecord.id == record_id,
            DiseaseRecord.status == "in_review", owned, ~cases, eligible
        ).values(status="expert_reviewed", expert_result=payload.treatment_suggestion, updated_at=utcnow()))
        if not result.rowcount:
            current = await DiseaseReviewService.record(db, record_id)
            receipt = current.review
            if (receipt and receipt.resolved_payload == values and receipt.assigned_expert_id == user.id
                    and receipt.assigned_subject == user.auth_subject and receipt.result_case_id):
                return DiseaseReviewService.response(receipt)
            raise HTTPException(409, "请先认领；仅当前有效认领人可提交一次复核，历史结论不能覆盖")
        try:
            review = await db.get(DiseaseReview, record_id, populate_existing=True)
            case = ExpertCase(disease_record_id=record_id, expert_id=user.id,
                confirmed_disease=payload.confirmed_disease, treatment_suggestion=payload.treatment_suggestion,
                notes=payload.notes, status="completed", is_knowledge_candidate=payload.is_knowledge_candidate)
            db.add(case); await db.flush()
            knowledge_id = None
            if payload.is_knowledge_candidate:
                knowledge_id = await DiseaseReviewService.add_knowledge(db, record, payload, user)
            review.result_case_id = case.id
            review.knowledge_item_id = knowledge_id
            review.resolved_payload = values
            review.resolved_expert_name = user.display_name or user.username
            review.resolved_at = utcnow()
            await db.commit()
            audit("disease_resolved", record_id=record_id, field_id=record.field_id, actor_id=user.id, expert_case_id=case.id)
            return DiseaseReviewService.response(review)
        except BaseException:
            await db.rollback()
            raise

    @staticmethod
    async def add_knowledge(db, record, payload, user):
        crop = record.crop_planting
        knowledge_id = "kb_" + uuid4().hex
        item = KnowledgeItem(id=knowledge_id, question=f"病害记录#{record.id}的专家复核：{payload.confirmed_disease}",
            answer=f"复核结论：{payload.confirmed_disease}\n处置建议：{payload.treatment_suggestion}\n复核说明：{payload.notes or '未填写'}",
            keywords=[payload.confirmed_disease, "专家复核"], category="专家案例", source_type="expert",
            source_name=f"专家账号【{user.display_name or user.username}】复核记录", authority_level="L4", is_current=1,
            crop=crop.crop if crop else None, growth_stage=crop.growth_stage if crop else None,
            topic="disease", disease=payload.confirmed_disease, version="v1.0")
        db.add(item); await db.flush()
        return knowledge_id

    @staticmethod
    def response(review):
        return ExpertResolveResponse(ticket_id=review.disease_record_id, status="expert_reviewed",
            confirmed_disease=review.resolved_payload["confirmed_disease"],
            treatment_suggestion=review.resolved_payload["treatment_suggestion"], expert_case_id=review.result_case_id,
            knowledge_item_id=review.knowledge_item_id,
            feedback_message="复核已保存，所属农户可在病害记录中查看。" + ("已保存L4专家案例；未自动重建向量索引。" if review.knowledge_item_id else ""))

    @staticmethod
    async def release_expert(db, user_id):
        ids = select(DiseaseReview.disease_record_id).where(DiseaseReview.assigned_expert_id == user_id,
            DiseaseReview.resolved_payload.is_(None))
        await db.execute(update(DiseaseRecord).where(DiseaseRecord.id.in_(ids), DiseaseRecord.status == "in_review")
            .values(status="pending_review", updated_at=utcnow()))
        await db.execute(update(DiseaseReview).where(DiseaseReview.assigned_expert_id == user_id,
            DiseaseReview.resolved_payload.is_(None)).values(assigned_expert_id=None, assigned_subject=None))
