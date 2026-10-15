"""Explicit archive and expert feedback contracts."""
from datetime import datetime
from uuid import UUID
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
from app.schemas.agent import AgentDecisionResponse
from app.schemas.consultation_review import OperationReview


class ConversationItem(BaseModel):
    id: str
    title: str
    field_id: Optional[int]
    field_ref: Optional[int]
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class AgentRequestStatus(BaseModel):
    request_id: str
    status: str
    conversation_id: str
    turn_id: str
    result: Optional[AgentDecisionResponse] = None
    message: str


class ConversationPage(BaseModel):
    items: List[ConversationItem]
    total: int


class ConsultationItem(BaseModel):
    id: str
    turn_id: str
    conversation_id: str
    query: str
    image_url: Optional[str] = None
    field_id: Optional[int]
    status: str
    assigned_expert_id: Optional[int]
    answer: Optional[str]
    decision: AgentDecisionResponse
    created_at: datetime
    updated_at: datetime
    claim_available: bool = False
    assigned_to_current_user: bool = False
    workflow_verified: bool = False
    reviewer_name: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    signed_review: Optional[OperationReview] = None
    request_id: Optional[str] = None


class ConsultationPage(BaseModel):
    items: List[ConsultationItem]
    total: int


class TurnItem(BaseModel):
    id: str
    query: str
    image_url: Optional[str]
    status: str
    decision: Optional[AgentDecisionResponse]
    error: Optional[str]
    created_at: datetime
    consultation: Optional[ConsultationItem] = None


class TurnPage(BaseModel):
    conversation: ConversationItem
    items: List[TurnItem]
    total: int


class ConsultationAnswer(BaseModel):
    answer: str = Field(..., min_length=1, max_length=6000)
    request_id: Optional[UUID] = None
    operation_review: Optional[OperationReview] = None

    @field_validator("answer")
    @classmethod
    def strip_answer(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("答复不能为空")
        return value
