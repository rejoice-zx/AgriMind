"""Expert Case Model - 专家复核与典型农业案例模型"""
from sqlalchemy import Column, Integer, String, Text, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class ExpertCase(Base):
    """专家复核与案例归档模型 - Human-in-the-Loop 闭环沉淀实体"""
    __tablename__ = "expert_cases"
    __table_args__ = (
        Index("ix_expert_cases_expert_status", "expert_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    disease_record_id = Column(Integer, ForeignKey("disease_records.id", ondelete="CASCADE"), nullable=False, index=True)
    expert_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    confirmed_disease = Column(String(100), nullable=False)  # 专家最终确诊病害
    treatment_suggestion = Column(Text, nullable=False)  # 综合防治处方与农事管理建议
    notes = Column(Text, nullable=True)  # 诊断手记与要点说明
    status = Column(String(20), default="completed")  # pending_review, completed, archived
    is_knowledge_candidate = Column(Integer, default=0)  # 专家明确选择后才生成知识案例
    created_at = Column(UTCDateTime, default=utcnow)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow)

    # 关联关系
    disease_record = relationship("DiseaseRecord", back_populates="expert_cases")
    expert = relationship("User", backref="reviewed_cases")

    def __repr__(self):
        return f"<ExpertCase(id={self.id}, disease='{self.confirmed_disease}', expert_id={self.expert_id})>"
