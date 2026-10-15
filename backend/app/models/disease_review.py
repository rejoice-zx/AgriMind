"""One assignment and one resolution receipt per independent disease record."""
from sqlalchemy import Column, Integer, String, JSON, ForeignKey
from sqlalchemy.orm import relationship
from app.core.model_base import Base
from app.utils.time import UTCDateTime, utcnow


class DiseaseReview(Base):
    __tablename__ = "disease_reviews"
    disease_record_id = Column(Integer, ForeignKey("disease_records.id", ondelete="CASCADE"), primary_key=True)
    assigned_expert_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    assigned_subject = Column(String(36), nullable=True)
    resolved_payload = Column(JSON(none_as_null=True), nullable=True)
    resolved_expert_name = Column(String(100), nullable=True)
    result_case_id = Column(Integer, ForeignKey("expert_cases.id", ondelete="SET NULL"), nullable=True)
    knowledge_item_id = Column(String(50), ForeignKey("knowledge_items.id", ondelete="SET NULL"), nullable=True)
    initial_crop = Column(String(64), nullable=True)
    image_features = Column(JSON, nullable=True)
    resolved_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    record = relationship("DiseaseRecord", back_populates="review")
    assigned_expert = relationship("User")
