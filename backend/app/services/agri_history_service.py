"""Persist questions before inference; replay completed requests without billing twice."""
import asyncio
import uuid
from app.core.audit_logging import audit, bind_audit
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select, func, update, insert, literal
from sqlalchemy.exc import IntegrityError
from app.models.agri_conversation import AgriConversation, AgriTurn, AgriConsultation
from app.models.user import User
from app.schemas.agent import AgentDecisionResponse
from app.schemas.agri_history import ConversationItem, ConsultationItem, TurnItem, TurnPage
from app.services.farm_service import FarmService
from app.services.agent_service import AgriAgentService
from app.core.roles import is_agri_staff
from app.utils.time import utcnow
from app.core.inference_budget import inference_budget, InferenceBudgetExceeded
from app.core.analysis_lifecycle import cancellable_analysis, AnalysisCancelled, signal_cancel


class AgriHistoryService:
    @staticmethod
    async def owned_conversation(db, conversation_id, user):
        row = await db.get(AgriConversation, str(conversation_id))
        owner_id = user if isinstance(user, int) else user.id
        if row is None or row.owner_id is None or row.owner_id != owner_id:
            raise HTTPException(404, "对话不存在")
        return row

    @staticmethod
    async def expire_pending(db, conversation_id):
        # A lost worker is observable, never silently retried by a history read.
        result = await db.execute(update(AgriTurn).where(
            AgriTurn.conversation_id == conversation_id, AgriTurn.status == "pending",
            AgriTurn.created_at < utcnow() - timedelta(minutes=30),
        ).values(status="failed", active_conversation_id=None, updated_at=utcnow(),
                 error="处理超时或服务中断，请核对记录后重新提问"))
        if result.rowcount:
            await db.commit()

    @staticmethod
    async def decide(db, request, user):
        if user is None or not getattr(user, "is_active", False) or getattr(user, "id", None) is None:
            raise HTTPException(401, "访问农业档案需要有效登录用户")
        user_id = user.id
        user_subject = user.auth_subject
        # Authorize all resources before persisting a question or calling any model.
        field_id = request.field_id
        if field_id is not None:
            await FarmService.ensure_field_writable(db, field_id, user)
        if request.crop_planting_id is not None:
            crop = await FarmService.ensure_crop_access(db, request.crop_planting_id, user)
            if field_id is not None and field_id != crop.field_id:
                raise HTTPException(400, "作物种植批次不属于指定地块")
            field_id = crop.field_id
            await FarmService.ensure_field_writable(db, field_id, user)
        payload = request.model_dump(mode="json", exclude={"request_id", "conversation_id"})
        request_id = str(request.request_id or uuid.uuid4())
        bind_audit(business_request_id=request_id, owner_id=user_id, field_id=field_id)
        previous = await db.scalar(select(AgriTurn).where(AgriTurn.request_id == request_id))
        if previous is not None:
            conversation = await AgriHistoryService.owned_conversation(db, previous.conversation_id, user)
            if previous.request_payload != payload or (request.conversation_id and str(request.conversation_id) != conversation.id):
                raise HTTPException(409, "请求编号已用于另一条提问，请勿更改重试内容")
            if previous.status == "completed":
                audit("decision_replayed", conversation_id=conversation.id, turn_id=previous.id)
                return AgentDecisionResponse.model_validate(previous.decision)
            raise HTTPException(409, "该提问仍在处理中或已失败，请刷新历史核对状态")
        if request.conversation_id:
            conversation = await AgriHistoryService.owned_conversation(db, request.conversation_id, user)
            if conversation.field_ref != field_id or (conversation.field_ref is not None and conversation.field_id is None):
                raise HTTPException(409, "对话绑定的田块已变更或删除，请新建对话")
            await AgriHistoryService.expire_pending(db, conversation.id)
        else:
            new_conversation_id = str(uuid.uuid4())
            eligible = select(User.id).where(User.id == user_id, User.auth_subject == user_subject,
                                             User.is_active.is_(True)).exists()
            # The ownership write checks account lifetime atomically, including ID reuse between auth and insert.
            result = await db.execute(insert(AgriConversation).from_select(
                ["id", "owner_id", "field_id", "field_ref", "title"],
                select(literal(new_conversation_id), literal(user_id), literal(field_id),
                       literal(field_id), literal(request.query.strip()[:100])).where(eligible)))
            if not result.rowcount:
                raise HTTPException(401, "账号状态已变化，请重新登录")
            conversation = await db.get(AgriConversation, new_conversation_id)
        conversation_id = conversation.id
        turn_id = str(uuid.uuid4())
        turn = AgriTurn(id=turn_id, conversation_id=conversation_id, request_id=request_id,
                        request_payload=payload, active_conversation_id=conversation_id,
                        query=request.query, image_url=request.image_url)
        db.add(turn)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            # Resolve a racing identical request without ever repeating inference.
            previous = await db.scalar(select(AgriTurn).where(AgriTurn.request_id == request_id))
            if previous is not None:
                await AgriHistoryService.owned_conversation(db, previous.conversation_id, user_id)
                if previous.request_payload == payload and previous.status == "completed" and (
                    not request.conversation_id or str(request.conversation_id) == previous.conversation_id
                ):
                    return AgentDecisionResponse.model_validate(previous.decision)
            raise HTTPException(409, "该对话已有处理中的提问，请刷新历史后再试")
        bind_audit(conversation_id=conversation_id, turn_id=turn_id)
        audit("decision_started", business_request_id=request_id, owner_id=user_id, field_id=field_id,
              conversation_id=conversation_id, turn_id=turn_id)
        try:
            async with inference_budget('agent'):
                decision = await cancellable_analysis(AgriAgentService.decide(db, request, current_user=user))
            # A stale worker cannot overwrite an expired request or create a ticket for it.
            active = select(User.id).where(User.id == user_id, User.auth_subject == user_subject, User.is_active.is_(True)).exists()
            result = await db.execute(update(AgriTurn).where(AgriTurn.id == turn_id, AgriTurn.status == "pending", active).values(
                status="completed", active_conversation_id=None, updated_at=utcnow()))
            if not result.rowcount:
                raise HTTPException(409, "本次处理已超时，请刷新历史核对")
            ticket = None
            if decision.need_expert:
                ticket = AgriConsultation(id=str(uuid.uuid4()), turn_id=turn_id)
                db.add(ticket)
            decision.conversation_id = conversation_id
            decision.turn_id = turn_id
            decision.consultation_id = ticket.id if ticket else None
            await db.execute(update(AgriTurn).where(AgriTurn.id == turn_id).values(decision=decision.model_dump(mode="json")))
            await db.execute(update(AgriConversation).where(AgriConversation.id == conversation_id).values(updated_at=utcnow()))
            await db.commit()
            bind_audit(consultation_id=decision.consultation_id)
            audit("decision_completed", business_request_id=request_id, conversation_id=conversation_id,
                  turn_id=turn_id, field_id=field_id, consultation_id=decision.consultation_id)
            return decision
        except (Exception, asyncio.CancelledError) as error:
            await db.rollback()
            await db.execute(update(AgriTurn).where(AgriTurn.id == turn_id, AgriTurn.status == "pending").values(
                status="failed", active_conversation_id=None, updated_at=utcnow(),
                error="本次分析超过处理时限，请核对历史；不会自动重试模型" if isinstance(error, InferenceBudgetExceeded) else
                    "用户已取消本次分析；未保存成功结论" if isinstance(error, AnalysisCancelled) else "本次分析未完成，请重新提问"))
            await db.commit()
            audit("decision_failed", business_request_id=request_id, conversation_id=conversation_id, turn_id=turn_id)
            if isinstance(error, InferenceBudgetExceeded):
                raise HTTPException(504, '本次分析超过处理时限，已停止本地工具链；请核对历史后再明确发起新提问') from None
            if isinstance(error, AnalysisCancelled):
                raise HTTPException(409, '本次分析已取消，请查询原回执') from None
            raise

    @staticmethod
    async def request_status(db, request_id, user):
        from app.services.diagnosis_media_service import DiagnosisMediaService
        from app.schemas.agri_history import AgentRequestStatus
        await DiagnosisMediaService.live_user(db, user)
        turn = await db.scalar(select(AgriTurn).where(AgriTurn.request_id == str(request_id)).execution_options(populate_existing=True))
        if turn is None:
            raise HTTPException(404, '原提问尚未登记或不存在，请稍后查询原编号')
        conversation = await AgriHistoryService.owned_conversation(db, turn.conversation_id, user)
        await AgriHistoryService.expire_pending(db, conversation.id)
        await db.refresh(turn)
        return AgentRequestStatus(request_id=str(request_id), status=turn.status,
            conversation_id=conversation.id, turn_id=turn.id,
            result=turn.decision if turn.status == 'completed' else None,
            message=turn.error or ('分析仍在处理中，断网后继续到处理时限；查询不会重跑模型' if turn.status == 'pending' else '返回原分析回执'))

    @staticmethod
    async def cancel_request(db, request_id, user):
        receipt = await AgriHistoryService.request_status(db, request_id, user)
        await db.execute(update(AgriTurn).where(AgriTurn.id == receipt.turn_id, AgriTurn.status == 'pending').values(
            status='failed', active_conversation_id=None, updated_at=utcnow(), error='用户已取消本次分析；未保存成功结论'))
        await db.commit()
        signal_cancel('agent', request_id, user.auth_subject)
        return await AgriHistoryService.request_status(db, request_id, user)

    @staticmethod
    def consultation_item(ticket, turn, conversation, viewer=None):
        from app.services.consultation_review_service import ConsultationReviewService
        review = ticket.review
        signed = review.resolved_payload if review and review.resolved_payload else None
        assigned = ConsultationReviewService.current_assignment(ticket)
        return ConsultationItem(id=ticket.id, turn_id=turn.id, conversation_id=conversation.id,
            query=turn.query, image_url=turn.image_url, field_id=conversation.field_id, status=ticket.status,
            assigned_expert_id=ticket.assigned_expert_id, answer=ticket.answer,
            decision=AgentDecisionResponse.model_validate(turn.decision),
            created_at=ticket.created_at, updated_at=ticket.updated_at,
            claim_available=ticket.status in {'pending', 'in_review'} and ticket.answer is None and not assigned and not signed,
            assigned_to_current_user=bool(assigned and viewer and is_agri_staff(viewer) and
                ticket.assigned_expert_id == viewer.id and review.assigned_subject == viewer.auth_subject),
            workflow_verified=bool(signed), reviewer_name=review.resolved_name if signed else None,
            reviewed_at=review.resolved_at if signed else None,
            signed_review=signed.get('operation_review') if signed else None,
            request_id=review.request_id if signed else None)

    @staticmethod
    async def turns(db, conversation_id, user, offset, limit):
        conversation = await AgriHistoryService.owned_conversation(db, conversation_id, user)
        await AgriHistoryService.expire_pending(db, conversation.id)
        where = AgriTurn.conversation_id == conversation.id
        total = await db.scalar(select(func.count()).select_from(AgriTurn).where(where))
        # Page 1 is the newest page, displayed chronologically within each page.
        rows = (await db.execute(select(AgriTurn, AgriConsultation).outerjoin(
            AgriConsultation, AgriConsultation.turn_id == AgriTurn.id).where(where)
            .order_by(AgriTurn.created_at.desc(), AgriTurn.id.desc()).offset(offset).limit(limit))).all()
        items = [TurnItem(id=turn.id, query=turn.query, image_url=turn.image_url, status=turn.status,
            decision=turn.decision, error=turn.error, created_at=turn.created_at,
            consultation=AgriHistoryService.consultation_item(ticket, turn, conversation, viewer=user) if ticket else None)
            for turn, ticket in reversed(rows)]
        return TurnPage(conversation=ConversationItem.model_validate(conversation), items=items, total=total)

    @staticmethod
    async def ticket(db, ticket_id, user):
        from app.services.diagnosis_media_service import DiagnosisMediaService
        current = await DiagnosisMediaService.live_user(db, user)
        row = (await db.execute(select(AgriConsultation, AgriTurn, AgriConversation)
            .join(AgriTurn, AgriTurn.id == AgriConsultation.turn_id)
            .join(AgriConversation, AgriConversation.id == AgriTurn.conversation_id)
            .where(AgriConsultation.id == str(ticket_id)).execution_options(populate_existing=True))).first()
        if row is None or (not is_agri_staff(current) and (row[2].owner_id is None or row[2].owner_id != current.id)):
            raise HTTPException(404, "咨询记录不存在")
        return row

    @staticmethod
    async def claim(db, ticket_id, user):
        from app.services.consultation_review_service import ConsultationReviewService
        await ConsultationReviewService.claim(db, ticket_id, user)
        return AgriHistoryService.consultation_item(*await AgriHistoryService.ticket(db, ticket_id, user), viewer=user)

    @staticmethod
    async def resolve(db, ticket_id, user, answer):
        from app.services.consultation_review_service import ConsultationReviewService
        await ConsultationReviewService.resolve(db, ticket_id, user, answer)
        return AgriHistoryService.consultation_item(*await AgriHistoryService.ticket(db, ticket_id, user), viewer=user)

    @staticmethod
    async def release_expert(db, user_id):
        from app.models.consultation_review import ConsultationReview
        await db.execute(update(ConsultationReview).where(ConsultationReview.consultation_id.in_(
            select(AgriConsultation.id).where(AgriConsultation.assigned_expert_id == user_id,
                AgriConsultation.status == 'in_review')), ConsultationReview.resolved_payload.is_(None)
        ).values(assigned_subject=None))
        await db.execute(update(AgriConsultation).where(
            AgriConsultation.assigned_expert_id == user_id, AgriConsultation.status == "in_review"
        ).values(status="pending", assigned_expert_id=None, updated_at=utcnow()))
