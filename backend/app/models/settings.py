"""System Settings Model"""
from sqlalchemy import Column, String, Text
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class SystemSettings(Base):
    """System settings database model - key-value store"""
    __tablename__ = "system_settings"
    
    key = Column(String(50), primary_key=True)
    value = Column(Text)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow)
    
    def __repr__(self):
        return f"<SystemSettings(key='{self.key}')>"
