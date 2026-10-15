# -*- coding: utf-8 -*-
"""Statistics persistence models."""

from sqlalchemy import Column, Date, Integer, String

from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class StatisticsDaily(Base):
    """Per-day statistics snapshot for cumulative and trend calculations."""

    __tablename__ = "statistics_daily"

    date = Column(Date, primary_key=True, index=True)
    conversations = Column(Integer, nullable=False, default=0)
    messages = Column(Integer, nullable=False, default=0)
    user_messages = Column(Integer, nullable=False, default=0)
    assistant_messages = Column(Integer, nullable=False, default=0)
    updated_at = Column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)


class StatisticsMeta(Base):
    """Metadata flags for statistics initialization lifecycle."""

    __tablename__ = "statistics_meta"

    key = Column(String(64), primary_key=True)
    value = Column(String(255), nullable=False)
    updated_at = Column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)
