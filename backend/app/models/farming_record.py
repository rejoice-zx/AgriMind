"""Farming Record Model - 农事操作记录模型"""
from sqlalchemy import Column, Integer, String, Float, Text, Date, ForeignKey, JSON, Index
from sqlalchemy.orm import relationship
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class FarmingRecord(Base):
    """农事活动记录模型（施肥、灌溉、打药、播种、采收等）"""
    __tablename__ = "farming_records"
    __table_args__ = (
        Index("ix_farming_records_field_date", "field_id", "record_date"),
        Index("ix_farming_records_date", "record_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(String(36), nullable=True, unique=True, index=True)
    request_fingerprint = Column(String(64), nullable=True)
    field_id = Column(Integer, ForeignKey("fields.id", ondelete="CASCADE"), nullable=False, index=True)
    crop_planting_id = Column(Integer, ForeignKey("crop_plantings.id", ondelete="SET NULL"), nullable=True, index=True)
    record_type = Column(String(50), nullable=False)  # 播种/移栽/施肥/灌溉/施药/修剪/采收/除草等
    record_date = Column(Date, nullable=False)  # 农事执行日期
    operator = Column(String(100), nullable=True)  # 执行人
    description = Column(Text, nullable=True)  # 详细说明
    input_name = Column(String(100), nullable=True)  # 投入品名称（如“复合肥15-15-15”、“吡虫啉”）
    input_amount = Column(Float, nullable=True)  # 投入数量
    unit = Column(String(20), nullable=True)  # 单位（kg/亩, ml/亩, 吨等）
    safety_interval_days = Column(Integer, nullable=True)  # 农药安全间隔期 (PHI, 天)
    attachments = Column(JSON, default=list)  # 照片附件列表等
    created_at = Column(UTCDateTime, default=utcnow)
    created_by_subject = Column(String(64), nullable=True)
    revoked_at = Column(UTCDateTime, nullable=True)
    revoked_by_subject = Column(String(64), nullable=True)
    revoke_reason = Column(String(500), nullable=True)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow)

    # 关联关系
    field = relationship("Field", back_populates="farming_records")
    crop_planting = relationship("CropPlanting", back_populates="farming_records")

    def __repr__(self):
        return f"<FarmingRecord(id={self.id}, type='{self.record_type}', date='{self.record_date}')>"


SPRAYING_TYPES = {"spraying", "施药", "病虫害防治", "pest_control", "植保", "打药", "喷药"}
FERTILIZING_TYPES = {"fertilizing", "施肥", "水肥管理", "fertilization", "追肥"}
IRRIGATION_TYPES = {"irrigation", "灌溉", "浇水"}
FIELD_MANAGEMENT_TYPES = {"field_management", "田间管理", "整枝", "抹杈", "授粉"}


def is_spraying_record(record_type: str) -> bool:
    """判定是否为施药/病虫害防治记录（兼容中英文多端值域）"""
    if not record_type:
        return False
    norm = str(record_type).strip().lower()
    return norm in SPRAYING_TYPES or any(s in norm for s in ["施药", "打药", "喷药", "spray", "pest_control", "病虫害"])


def is_fertilizing_record(record_type: str) -> bool:
    """判定是否为施肥/水肥管理记录（兼容中英文多端值域）"""
    if not record_type:
        return False
    norm = str(record_type).strip().lower()
    return norm in FERTILIZING_TYPES or any(s in norm for s in ["施肥", "水肥", "fertiliz"])
