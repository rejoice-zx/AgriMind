"""Reserve before inference; never rerun ambiguous requests or commit a case without its result receipt."""
import asyncio
import logging
from app.core.audit_logging import bind_audit, audit
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select, update, insert, literal
from sqlalchemy.exc import IntegrityError
from app.models import User, Field, Farm
from app.models.diagnosis_media import DiagnosisRequest
from app.schemas.expert import VisualDiagnosisResponse, DiagnosisRequestStatus
from app.services.diagnosis_media_service import DiagnosisMediaService
from app.services.farm_service import FarmService
from app.services.expert_service import ExpertWorkbenchService
from app.core.roles import AGRI_VALUES
from app.utils.time import utcnow
from app.core.inference_budget import inference_budget, InferenceBudgetExceeded
from app.core.analysis_lifecycle import cancellable_analysis, AnalysisCancelled, signal_cancel


logger = logging.getLogger(__name__)


class DiagnosisRequestService:
    @staticmethod
    def actor(user):
        return SimpleNamespace(id=user.id, auth_subject=user.auth_subject, role=user.role, is_active=user.is_active)

    @classmethod
    async def owned(cls, db, request_id, actor):
        current = await DiagnosisMediaService.live_user(db, actor)
        job = await db.get(DiagnosisRequest, str(request_id), populate_existing=True)
        if job is None or job.owner_id != actor.id or job.owner_subject != actor.auth_subject:
            raise HTTPException(404, "诊断请求不存在或归属已变化")
        field_ref = job.payload.get("field_id")
        if field_ref is not None:
            if job.field_id != field_ref: raise HTTPException(409, "原田块已删除，本次不重新执行诊断")
            field = await FarmService.ensure_field_access(db, field_ref, current)
            if field.created_at != job.field_created_at: raise HTTPException(409, "田块归属档案已变化，本次不重新执行诊断")
        await DiagnosisMediaService.access(db, job.payload["image_url"], actor)
        if job.result and job.result.get("disease_record_id") is not None and job.record_id is None:
            raise HTTPException(409, "原病例已移除，不能把历史回复当作现存病例")
        return job

    @staticmethod
    def response(job, replayed=True):
        result = VisualDiagnosisResponse.model_validate(job.result)
        # Older receipts stored transport failures as completed diagnoses. Surface
        # that failure honestly without rewriting existing cases or request IDs.
        if result.suspected_disease in {"视觉模型离线/网络受阻", "无法判断（视觉输出格式异常）",
                "待农技专家人工确诊 (API Key 未配置)"} and result.error_code is None:
            result = result.model_copy(update={"diagnosis_status": "failed", "error_code": "legacy_failure",
                "error_message": "这是此前保存的未完成诊断，具体失败原因未记录；可明确发起新的诊断。"})
        return result.model_copy(update={"request_id": job.id, "replayed": replayed, "processed_at": job.updated_at})

    @classmethod
    async def status(cls, db, request_id, user):
        job = await cls.owned(db, request_id, cls.actor(user))
        state = "needs_review" if job.status == "pending" and job.created_at < utcnow() - timedelta(minutes=10) else job.status
        result = cls.response(job) if job.status in {"completed", "failed"} and job.result else None
        if result is not None and result.diagnosis_status == "failed": state = "failed"
        return DiagnosisRequestStatus(request_id=job.id, status=state,
            result=result,
            message={"pending": "诊断仍在处理中，可刷新查询；不会再次调用模型", "needs_review": "处理状态待核对，不自动重跑；请管理员核查",
                     "failed": "本次未取得有效诊断；如需重新分析，请明确开始新的诊断", "completed": "返回已保存的初诊结果"}[state])

    @classmethod
    async def run(cls, db, request, user):
        actor = cls.actor(user)
        field = None
        if request.field_id is not None: field = await FarmService.ensure_field_access(db, request.field_id, actor)
        await DiagnosisMediaService.access(db, request.image_url, actor, for_link=True, field_id=request.field_id)
        request_id = str(request.request_id or uuid4())
        bind_audit(business_request_id=request_id, owner_id=actor.id, field_id=request.field_id)
        payload = request.model_dump(mode="json", exclude={"request_id"})
        job = await db.get(DiagnosisRequest, request_id)
        if job is not None:
            job = await cls.owned(db, request_id, actor)
            stored_payload = {"crop_planting_id": None, **job.payload}
            if stored_payload != payload: raise HTTPException(409, "同一请求ID不能用于不同照片、田块或诊断内容")
            if job.status in {"completed", "failed"} and job.result:
                audit("diagnosis_replayed", business_request_id=request_id, record_id=job.record_id, field_id=request.field_id)
                return cls.response(job)
            raise HTTPException(409, "该诊断已提交，请查询原请求状态；不会再次调用模型")
        if field is not None:
            await FarmService.ensure_field_writable(db, field.id, actor)
        active = select(User.id).where(User.id == actor.id, User.auth_subject == actor.auth_subject, User.is_active.is_(True)).exists()
        values = {"id": request_id, "owner_id": actor.id, "owner_subject": actor.auth_subject, "payload": payload,
            "field_id": field.id if field else None, "field_created_at": field.created_at if field else None,
            "status": "pending", "created_at": utcnow(), "updated_at": utcnow()}
        guards = [active]
        if field:
            guards.append(select(Field.id).join(Farm).join(User, User.id == actor.id).where(Field.id == field.id,
                Field.created_at == field.created_at,
                (User.role.in_(AGRI_VALUES) | (Farm.owner_id == User.id))).exists())
        try:
            inserted = await db.scalar(insert(DiagnosisRequest).from_select(list(values),
                select(*[literal(value, type_=DiagnosisRequest.__table__.c[key].type) for key, value in values.items()]).where(*guards)).returning(DiagnosisRequest.id))
            if inserted is None: raise HTTPException(409, "账号或田块权限已变化，本次未执行诊断")
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return await cls.run(db, request.model_copy(update={"request_id": request_id}), user)
        audit("diagnosis_started", business_request_id=request_id, owner_id=actor.id, field_id=request.field_id)
        logger.info("diagnosis_request_started request_id=%s owner_id=%s field_id=%s", request_id, actor.id, request.field_id)
        try:
            async with inference_budget('diagnosis'):
                result = await cancellable_analysis(ExpertWorkbenchService.visual_diagnose(db, request, current_user=actor, commit=False))
            result.request_id = request_id
            result.processed_at = utcnow()
            outcome = result.diagnosis_status
            saved = await db.execute(update(DiagnosisRequest).where(DiagnosisRequest.id == request_id,
                DiagnosisRequest.status == "pending", DiagnosisRequest.owner_id == actor.id,
                DiagnosisRequest.owner_subject == actor.auth_subject, active).values(status=outcome,
                result=result.model_dump(mode="json"), record_id=result.disease_record_id, updated_at=result.processed_at))
            if saved.rowcount != 1: raise HTTPException(409, "账号或请求状态已变化，本次未保存诊断")
            await db.commit()
            bind_audit(record_id=result.disease_record_id)
            audit("diagnosis_" + outcome, business_request_id=request_id, field_id=request.field_id, record_id=result.disease_record_id)
            logger.info("diagnosis_request_%s request_id=%s record_id=%s", outcome, request_id, result.disease_record_id)
            return result
        except (Exception, asyncio.CancelledError) as error:
            await db.rollback()
            await db.execute(update(DiagnosisRequest).where(DiagnosisRequest.id == request_id,
                DiagnosisRequest.status == "pending").values(status="failed", updated_at=utcnow()))
            await db.commit()
            audit("diagnosis_failed", business_request_id=request_id, field_id=request.field_id)
            logger.warning("diagnosis_request_failed request_id=%s error_type=%s", request_id, type(error).__name__)
            if isinstance(error, InferenceBudgetExceeded):
                raise HTTPException(504, '本次诊断超过处理时限，已停止本地工具链；请查询原请求状态后再明确开始新诊断') from None
            if isinstance(error, AnalysisCancelled):
                raise HTTPException(409, '本次诊断已取消，请查询原回执') from None
            raise

    @classmethod
    async def cancel(cls, db, request_id, user):
        job = await cls.owned(db, request_id, cls.actor(user))
        failure = VisualDiagnosisResponse(diagnosis_status='failed', error_code='cancelled',
            error_message='用户已取消本次分析；未保存成功病例', request_id=str(request_id), processed_at=utcnow(),
            image_url=job.payload['image_url'], crop=job.payload.get('crop') or '未提供',
            suspected_disease='未完成诊断', need_expert=True, advice='如需重新分析，请明确开始新的诊断')
        await db.execute(update(DiagnosisRequest).where(DiagnosisRequest.id == str(request_id),
            DiagnosisRequest.status == 'pending').values(status='failed', updated_at=utcnow(), result=failure.model_dump(mode='json')))
        await db.commit()
        signal_cancel('diagnosis', request_id, user.auth_subject)
        return await cls.status(db, request_id, user)
