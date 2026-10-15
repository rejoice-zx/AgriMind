"""Media ownership and durable request receipts; both survive account deletion without ID rebinding."""
from sqlalchemy import Column, String, Integer, JSON, ForeignKey
from app.core.model_base import Base
from app.utils.time import UTCDateTime, utcnow


class DiagnosisMedia(Base):
    __tablename__ = "diagnosis_media"
    id = Column(String(80), primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    content_type = Column(String(40), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)


class DiagnosisRequest(Base):
    __tablename__ = "diagnosis_requests"
    id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    owner_subject = Column(String(36), nullable=False)
    payload = Column(JSON, nullable=False)
    field_id = Column(Integer, ForeignKey("fields.id", ondelete="SET NULL"), nullable=True)
    field_created_at = Column(UTCDateTime, nullable=True)
    record_id = Column(Integer, ForeignKey("disease_records.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), default="pending", nullable=False)
    result = Column(JSON, nullable=True)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    updated_at = Column(UTCDateTime, default=utcnow, nullable=False)
