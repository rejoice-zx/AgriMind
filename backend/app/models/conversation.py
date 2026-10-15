"""Conversation and Message Models"""
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class Conversation(Base):
    """Conversation database model"""
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_status_updated_at", "status", "updated_at"),
        Index("ix_conversations_created_at", "created_at"),
    )
    
    id = Column(String(36), primary_key=True, index=True)
    title = Column(String(200))
    status = Column(String(20), default="normal")  # normal, pending_human, human_handling, human_closed
    customer_id = Column(String(64), nullable=False, index=True)  # visitor_xxx or user_xxx
    temp_session_id = Column(String(128), nullable=True, index=True)  # guest local session id for merge dedupe
    temp_device_id = Column(String(128), nullable=True, index=True)  # guest device id for merge dedupe
    human_agent_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    is_deleted_by_user = Column(Boolean, default=False, nullable=False, index=True)
    deleted_by_customer_id = Column(String(64), nullable=True)
    deleted_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=utcnow)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow)
    
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Conversation(id='{self.id}', title='{self.title}', status='{self.status}')>"


class Message(Base):
    """Message database model"""
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_timestamp", "timestamp"),
        Index("ix_messages_role_timestamp", "role", "timestamp"),
        Index("ix_messages_conversation_deleted_timestamp", "conversation_id", "is_deleted_by_user", "timestamp"),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # user, assistant, human
    content = Column(Text, nullable=False)
    confidence = Column(Float, nullable=True)
    rag_trace = Column(JSON, nullable=True)
    human_agent_name = Column(String(100), nullable=True)  # display name of human agent
    is_deleted_by_user = Column(Boolean, default=False, nullable=False, index=True)
    deleted_by_customer_id = Column(String(64), nullable=True, index=True)
    deleted_at = Column(UTCDateTime, nullable=True)
    timestamp = Column(UTCDateTime, default=utcnow)
    
    conversation = relationship("Conversation", back_populates="messages")
    
    def __repr__(self):
        return f"<Message(id={self.id}, role='{self.role}', conversation_id='{self.conversation_id}')>"
