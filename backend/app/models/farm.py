"""Farm Model - 农业智能系统农场模型"""
from sqlalchemy import Column, Integer, String, Float, Text, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class Farm(Base):
    """农场数据库模型"""
    __tablename__ = "farms"
    __table_args__ = (
        Index("ix_farms_owner_created", "owner_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    province = Column(String(50), nullable=True)
    city = Column(String(50), nullable=True)
    district = Column(String(50), nullable=True)
    address = Column(String(255), nullable=True)
    region_code = Column(String(12), nullable=True)
    location_source = Column(String(30), nullable=True)
    location_label = Column(String(255), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    area = Column(Float, default=0.0)  # 占地面积（亩）
    created_at = Column(UTCDateTime, default=utcnow)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow)
    archived_at = Column(UTCDateTime, nullable=True)

    # 关联关系
    owner = relationship("User", backref="farms")
    fields = relationship("Field", back_populates="farm", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Farm(id={self.id}, name='{self.name}', owner_id={self.owner_id})>"
