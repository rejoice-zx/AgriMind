"""Append-only, explicitly timed manual observations, distinct from provider weather."""
from sqlalchemy import Column, Integer, String, Float, Text, ForeignKey, Index
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class FieldEnvironmentObservation(Base):
    __tablename__ = 'field_environment_observations'
    __table_args__ = (Index('ix_field_environment_latest', 'field_id', 'observed_at', 'id'),)
    id = Column(Integer, primary_key=True)
    field_id = Column(Integer, ForeignKey('fields.id'), nullable=False)
    request_id = Column(String(36), nullable=False, unique=True)
    request_fingerprint = Column(String(64), nullable=False)
    created_by_subject = Column(String(64), nullable=False)
    facility_type = Column(String(20), nullable=False)
    measurement_scope = Column(String(24), nullable=False)
    observed_at = Column(UTCDateTime, nullable=False)
    temperature = Column(Float, nullable=True)
    humidity = Column(Float, nullable=True)
    ventilation = Column(String(16), nullable=False, default='unknown')
    covering = Column(String(16), nullable=False, default='unknown')
    notes = Column(Text, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=utcnow)
