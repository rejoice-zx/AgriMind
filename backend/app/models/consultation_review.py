"""Account-lifetime assignment and immutable signed consultation receipts."""
from sqlalchemy import Column, String, Integer, JSON, ForeignKey
from app.core.model_base import Base
from app.utils.time import UTCDateTime


class ConsultationReview(Base):
    __tablename__ = 'agri_consultation_reviews'
    consultation_id = Column(String(36), ForeignKey('agri_consultations.id', ondelete='RESTRICT'), primary_key=True)
    assigned_subject = Column(String(36), nullable=True)
    request_id = Column(String(36), nullable=True, unique=True)
    resolved_payload = Column(JSON(none_as_null=True), nullable=True)
    context_snapshot = Column(JSON(none_as_null=True), nullable=True)
    resolved_by_id = Column(Integer, nullable=True)  # Historical identity survives account removal.
    resolved_subject = Column(String(36), nullable=True)
    resolved_name = Column(String(100), nullable=True)
    resolved_at = Column(UTCDateTime, nullable=True)
