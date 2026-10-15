"""Crop Planting Model - 作物种植批次模型"""
from sqlalchemy import Column, Integer, String, Float, ForeignKey, Index, Date
from sqlalchemy.orm import relationship
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class CropPlanting(Base):
    """地块作物种植批次与生长档案"""
    __tablename__ = "crop_plantings"
    __table_args__ = (
        Index("ix_crop_plantings_field_status", "field_id", "status"),
        Index("ix_crop_plantings_crop_stage", "crop", "growth_stage"),
    )

    id = Column(Integer, primary_key=True, index=True)
    field_id = Column(Integer, ForeignKey("fields.id", ondelete="CASCADE"), nullable=False, index=True)
    crop = Column(String(50), nullable=False)  # 作物名称，如“番茄”
    variety = Column(String(50), nullable=True)  # 品种，如“普罗旺斯”
    planting_date = Column(Date, nullable=False)  # 播种/定植日期
    expected_harvest_date = Column(Date, nullable=True)  # 预期收获日期
    growth_stage = Column(String(50), nullable=False, default="苗期")  # 苗期/开花期/坐果期/采收期等
    area = Column(Float, default=0.0)  # 种植面积（亩）
    status = Column(String(20), default="active")  # active (生长中), harvested (已收获), terminated (已中止)
    created_at = Column(UTCDateTime, default=utcnow)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow)

    # 关联关系
    field = relationship("Field", back_populates="crop_plantings")
    farming_records = relationship("FarmingRecord", back_populates="crop_planting", cascade="all, delete-orphan")
    disease_records = relationship("DiseaseRecord", back_populates="crop_planting", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<CropPlanting(id={self.id}, crop='{self.crop}', stage='{self.growth_stage}', field_id={self.field_id})>"
