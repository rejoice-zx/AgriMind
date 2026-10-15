"""Agricultural archives, independent of retired ecommerce sessions."""
import uuid
from sqlalchemy import Column, String, Integer, Text, JSON, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.core.model_base import Base
from app.utils.time import UTCDateTime, utcnow


def new_id():
    return str(uuid.uuid4())


class AgriConversation(Base):
    __tablename__ = "agri_conversations"
    __table_args__ = (Index("ix_agri_conversations_owner_updated", "owner_id", "updated_at"),
                     Index("ix_agri_conversations_created", "created_at"))
    id = Column(String(36), primary_key=True, default=new_id)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    field_id = Column(Integer, ForeignKey("fields.id", ondelete="SET NULL"), nullable=True)
    field_ref = Column(Integer, nullable=True)  # Original binding survives deletion; never rebind history.
    title = Column(String(100), nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    updated_at = Column(UTCDateTime, default=utcnow, nullable=False)


class AgriTurn(Base):
    __tablename__ = "agri_turns"
    __table_args__ = (Index("ix_agri_turns_conversation_date", "conversation_id", "created_at"),
                     Index("ix_agri_turns_created_status", "created_at", "status"))
    id = Column(String(36), primary_key=True, default=new_id)
    conversation_id = Column(String(36), ForeignKey("agri_conversations.id", ondelete="CASCADE"), nullable=False)
    request_id = Column(String(36), nullable=False, unique=True)
    request_payload = Column(JSON, nullable=False)
    # Only one running request per conversation. Multiple NULLs are allowed after completion.
    active_conversation_id = Column(String(36), nullable=True, unique=True)
    query = Column(Text, nullable=False)
    image_url = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    decision = Column(JSON, nullable=True)
    error = Column(String(200), nullable=True)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    updated_at = Column(UTCDateTime, default=utcnow, nullable=False)


class AgriConsultation(Base):
    __tablename__ = "agri_consultations"
    __table_args__ = (Index("ix_agri_consultations_status_date", "status", "created_at"),
                     Index("ix_agri_consultations_created_status", "created_at", "status"),
                     Index("ix_agri_consultations_updated_status", "updated_at", "status"))
    id = Column(String(36), primary_key=True, default=new_id)
    turn_id = Column(String(36), ForeignKey("agri_turns.id", ondelete="CASCADE"), nullable=False, unique=True)
    status = Column(String(20), nullable=False, default="pending")
    assigned_expert_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    answer = Column(Text, nullable=True)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    updated_at = Column(UTCDateTime, default=utcnow, nullable=False)
    review = relationship('ConsultationReview', uselist=False, lazy='selectin', passive_deletes='all')
    assigned_expert = relationship('User', lazy='selectin')
