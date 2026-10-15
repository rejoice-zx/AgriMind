"""Disease Record Model - 作物病虫害记录与初诊模型"""
from sqlalchemy import Column, Integer, String, Float, Text, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class DiseaseRecord(Base):
    """病虫害记录与初诊模型"""
    __tablename__ = "disease_records"
    __table_args__ = (
        Index("ix_disease_records_field_date", "field_id", "created_at"),
        Index("ix_disease_records_name", "disease_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    field_id = Column(Integer, ForeignKey("fields.id", ondelete="CASCADE"), nullable=False, index=True)
    crop_planting_id = Column(Integer, ForeignKey("crop_plantings.id", ondelete="SET NULL"), nullable=True, index=True)
    disease_name = Column(String(100), nullable=False)  # 病害名称，如“番茄叶霉病”
    symptoms = Column(Text, nullable=True)  # 表现症状描述
    severity = Column(String(20), default="unknown")  # unknown, low, medium, high, severe
    diagnosis_method = Column(String(50), default="unknown")  # unknown, manual, ai_vision, expert, lab
    diagnosis_confidence = Column(Float, nullable=True)  # 置信度 (0.0 ~ 1.0)
    image_url = Column(String(500), nullable=True)  # 病害叶片/植株图片地址
    expert_result = Column(Text, nullable=True)  # 专家复核结论
    status = Column(String(20), default="preliminary")  # preliminary (初诊), expert_reviewed (专家复核), resolved (已解决)
    created_at = Column(UTCDateTime, default=utcnow)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow)

    # 关联关系
    field = relationship("Field", back_populates="disease_records")
    crop_planting = relationship("CropPlanting", back_populates="disease_records")
    expert_cases = relationship("ExpertCase", back_populates="disease_record", cascade="all, delete-orphan")
    review = relationship("DiseaseReview", back_populates="record", uselist=False, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<DiseaseRecord(id={self.id}, disease='{self.disease_name}', confidence={self.diagnosis_confidence})>"
