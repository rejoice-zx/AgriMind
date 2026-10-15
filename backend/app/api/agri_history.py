"""Owned agricultural history and general consultations (not disease diagnoses)."""
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from types import SimpleNamespace
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.api.dependencies import get_current_user, require_roles
from app.core.roles import AGRI_VALUES
from app.core.weather_policy import AGRI_TIMEZONE
from app.models.user import User
from app.models.agri_conversation import AgriConversation, AgriTurn, AgriConsultation
from app.schemas.agri_history import (ConversationPage, ConversationItem, TurnPage,
    ConsultationItem, ConsultationPage, ConsultationAnswer)
from app.services.agri_history_service import AgriHistoryService
from app.core.audit_logging import audit

router = APIRouter(prefix="/api", tags=["农业对话档案与专家咨询"])
agri_staff = require_roles(sorted(AGRI_VALUES))


@router.get("/agent/conversations", response_model=ConversationPage)
async def list_conversations(q: str = Query("", max_length=200), field_id: int | None = None,
    date_from: date | None = None, date_to: date | None = None,
    offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "起始日期不能晚于结束日期")
    conditions = [AgriConversation.owner_id == user.id]
    if q.strip():
        conditions.append(or_(AgriConversation.title.contains(q.strip(), autoescape=True),
            AgriConversation.id.in_(select(AgriTurn.conversation_id).where(AgriTurn.query.contains(q.strip(), autoescape=True)))))
    if field_id is not None:
        conditions.append(AgriConversation.field_ref == field_id)
    if date_from and date_from != date.min:
        conditions.append(AgriConversation.updated_at >= datetime.combine(date_from, time.min, AGRI_TIMEZONE).astimezone(timezone.utc))
    if date_to:
        if date_to == date.max:
            conditions.append(AgriConversation.updated_at <= datetime.combine(date_to, time.max, AGRI_TIMEZONE).astimezone(timezone.utc))
        else:
            conditions.append(AgriConversation.updated_at < datetime.combine(date_to + timedelta(days=1), time.min, AGRI_TIMEZONE).astimezone(timezone.utc))
    total = await db.scalar(select(func.count()).select_from(AgriConversation).where(*conditions))
    items = (await db.scalars(select(AgriConversation).where(*conditions)
        .order_by(AgriConversation.updated_at.desc(), AgriConversation.id.desc()).offset(offset).limit(limit))).all()
    return ConversationPage(total=total, items=[ConversationItem.model_validate(item) for item in items])


@router.get("/agent/conversations/{conversation_id}", response_model=TurnPage)
async def get_conversation(conversation_id: UUID, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return await AgriHistoryService.turns(db, str(conversation_id), user, offset, limit)


@router.post("/agent/turns/{turn_id}/consultation", response_model=ConsultationItem)
async def transfer_turn(turn_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    actor = SimpleNamespace(id=user.id, role=user.role, auth_subject=user.auth_subject, is_active=user.is_active)
    turn = await db.get(AgriTurn, str(turn_id))
    if turn is None:
        raise HTTPException(404, "提问不存在")
    await AgriHistoryService.owned_conversation(db, turn.conversation_id, user)
    if turn.status != "completed":
        raise HTTPException(409, "只能转交已完成的分析")
    ticket = await db.scalar(select(AgriConsultation).where(AgriConsultation.turn_id == turn.id))
    if ticket is None:
        ticket = AgriConsultation(turn_id=turn.id)
        db.add(ticket)
        try:
            await db.flush()
            # Persist ticket id in the original response so replay and history stay consistent.
            decision = dict(turn.decision)
            decision["consultation_id"] = ticket.id
            turn.decision = decision
            await db.commit()
            audit("consultation_requested", consultation_id=ticket.id, conversation_id=turn.conversation_id,
                  turn_id=turn.id, actor_id=user.id)
        except IntegrityError:
            await db.rollback()
            ticket = await db.scalar(select(AgriConsultation).where(AgriConsultation.turn_id == str(turn_id)))
            if ticket is None:
                raise HTTPException(409, "转交失败，请刷新后重试")
    return AgriHistoryService.consultation_item(*await AgriHistoryService.ticket(db, ticket.id, actor), viewer=actor)


@router.get("/agri-consultations", response_model=ConsultationPage)
async def list_consultations(status: str | None = Query(None, pattern="^(pending|in_review|resolved)$"),
    offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db), user: User = Depends(agri_staff)):
    conditions = [AgriConsultation.status == status] if status else []
    total = await db.scalar(select(func.count()).select_from(AgriConsultation).where(*conditions))
    rows = (await db.execute(select(AgriConsultation, AgriTurn, AgriConversation)
        .join(AgriTurn, AgriTurn.id == AgriConsultation.turn_id)
        .join(AgriConversation, AgriConversation.id == AgriTurn.conversation_id)
        .where(*conditions).order_by(AgriConsultation.created_at.desc(), AgriConsultation.id.desc())
        .offset(offset).limit(limit).execution_options(populate_existing=True))).all()
    return ConsultationPage(total=total, items=[AgriHistoryService.consultation_item(*row, viewer=user) for row in rows])


@router.get("/agri-consultations/{ticket_id}", response_model=ConsultationItem)
async def get_consultation(ticket_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return AgriHistoryService.consultation_item(*await AgriHistoryService.ticket(db, str(ticket_id), user), viewer=user)


@router.get("/agri-consultations/{ticket_id}/review-context")
async def get_review_context(ticket_id: UUID, crop_planting_id: int | None = Query(None, ge=1),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    from app.services.consultation_review_service import ConsultationReviewService
    return await ConsultationReviewService.context(db, str(ticket_id), user, crop_planting_id)


@router.post("/agri-consultations/{ticket_id}/claim", response_model=ConsultationItem)
async def claim_consultation(ticket_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(agri_staff)):
    return await AgriHistoryService.claim(db, str(ticket_id), user)


@router.post("/agri-consultations/{ticket_id}/resolve", response_model=ConsultationItem)
async def resolve_consultation(ticket_id: UUID, request: ConsultationAnswer,
    db: AsyncSession = Depends(get_db), user: User = Depends(agri_staff)):
    return await AgriHistoryService.resolve(db, str(ticket_id), user, request)
