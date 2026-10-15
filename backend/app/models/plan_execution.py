"""Unique receipts for confirmed daily plan records; survives restarts and concurrent retries."""
from sqlalchemy import Column, String, Integer, ForeignKey
from app.core.model_base import Base


class PlanExecution(Base):
    __tablename__ = "plan_executions"
    fingerprint = Column(String(64), primary_key=True)
    field_id = Column(Integer, ForeignKey("fields.id", ondelete="CASCADE"), nullable=False)
    record_id = Column(Integer, ForeignKey("farming_records.id", ondelete="SET NULL"), nullable=True)
