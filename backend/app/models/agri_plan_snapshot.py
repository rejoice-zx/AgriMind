"""One durable rolling checklist per planting; confirmation proofs are never persisted."""
from sqlalchemy import Column, Integer, ForeignKey, Date, JSON, String
from app.core.model_base import Base
from app.utils.time import UTCDateTime


class AgriPlanSnapshot(Base):
    __tablename__ = 'agri_plan_snapshots'
    crop_planting_id = Column(Integer, ForeignKey('crop_plantings.id', ondelete='CASCADE'), primary_key=True)
    field_id = Column(Integer, ForeignKey('fields.id', ondelete='CASCADE'), nullable=False, index=True)
    anchor_date = Column(Date, nullable=False)
    input_hash = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    evaluated_at = Column(UTCDateTime, nullable=False)
    payload = Column(JSON, nullable=False)
