"""Field Model - 农田/地块数据模型"""
from sqlalchemy import Column, Integer, String, Float, Text, ForeignKey
from sqlalchemy.orm import relationship
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class Field(Base):
    """地块数据库模型 - 系统的核心时空实体"""
    __tablename__ = "fields"

    id = Column(Integer, primary_key=True, index=True)
    farm_id = Column(Integer, ForeignKey("farms.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    area = Column(Float, default=0.0)  # 地块面积（亩）
    soil_type = Column(String(50), nullable=True)  # 壤土、砂土、黏土等
    irrigation_type = Column(String(50), nullable=True)  # 滴灌、喷灌、漫灌等
    facility_type = Column(String(20), nullable=False, default='unknown', server_default='unknown')
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    remark = Column(Text, nullable=True)
    created_at = Column(UTCDateTime, default=utcnow)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow)
    archived_at = Column(UTCDateTime, nullable=True)

    # 关联关系
    farm = relationship("Farm", back_populates="fields")
    crop_plantings = relationship("CropPlanting", back_populates="field", cascade="all, delete-orphan")
    farming_records = relationship("FarmingRecord", back_populates="field", cascade="all, delete-orphan")
    disease_records = relationship("DiseaseRecord", back_populates="field", cascade="all, delete-orphan")
    weather_snapshots = relationship("WeatherSnapshot", back_populates="field", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Field(id={self.id}, name='{self.name}', farm_id={self.farm_id})>"
