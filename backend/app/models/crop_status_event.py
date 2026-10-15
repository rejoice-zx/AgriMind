"""Append-only crop state history; reopening requires a separate, reasoned command."""
from sqlalchemy import Column, Integer, String, Text, ForeignKey, Index
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class CropStatusEvent(Base):
    __tablename__ = 'crop_status_events'
    __table_args__ = (Index('ix_crop_status_events_crop_id', 'crop_planting_id', 'id'),)
    id = Column(Integer, primary_key=True)
    crop_planting_id = Column(Integer, ForeignKey('crop_plantings.id'), nullable=False)
    previous_status = Column(String(20), nullable=False)
    new_status = Column(String(20), nullable=False)
    reason = Column(Text, nullable=True)
    created_by_subject = Column(String(64), nullable=False)
    actor_name = Column(String(100), nullable=False)
    request_id = Column(String(36), nullable=True, unique=True)
    request_fingerprint = Column(String(64), nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=utcnow)
