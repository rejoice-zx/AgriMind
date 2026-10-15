"""Knowledge Item Model - 农业与全域知识库模型"""
from sqlalchemy import Column, String, Text, Float, JSON, Integer, Index
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class KnowledgeItem(Base):
    """Knowledge item database model with agriculture metadata and version control"""
    __tablename__ = "knowledge_items"
    __table_args__ = (
        Index("ix_knowledge_crop_stage_topic", "crop", "growth_stage", "topic"),
        Index("ix_knowledge_crop_disease", "crop", "disease"),
    )
    
    id = Column(String(50), primary_key=True, index=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    keywords = Column(JSON, default=list)
    category = Column(String(50), default="通用", index=True)
    score = Column(Float, default=1.0)
    
    # 农业领域专属 Metadata
    crop = Column(String(50), nullable=True, index=True)  # 作物名称，如“番茄”
    region = Column(String(50), nullable=True, index=True)  # 适用地区，如“江苏”
    growth_stage = Column(String(50), nullable=True, index=True)  # 生长阶段，如“开花期”
    topic = Column(String(50), nullable=True, index=True)  # 主题: disease, pest, fertilizer, irrigation, cultivation, standard
    disease = Column(String(100), nullable=True, index=True)  # 具体病害名称，如“番茄叶霉病”
    
    # 知识溯源与分层管理 (L1 权威规范 > L2 专业文献 > L3 农技资料 > L4 专家案例)
    source_type = Column(String(50), default="unverified")  # unverified, quarantined, standard, literature, extension, expert
    source_name = Column(String(200), nullable=True)  # 原文/产品标签上的完整来源名称，缺失保持未知
    authority_level = Column(String(10), default="未核验")  # 来源分级标注，不构成真实性或适用性核验
    publish_date = Column(String(20), nullable=True)  # 规范/文献发布日期，如“2020-07-27”
    url = Column(String(500), nullable=True)  # 权威溯源链接
    source_updated_at = Column(String(20), nullable=True)  # 规范修订更新日期，如“2020-07-27”
    
    # 知识版本控制
    version = Column(String(20), nullable=True)  # 来源版本，未提供时不推测
    is_current = Column(Integer, default=1, index=True)  # 1: 当前生效最新版, 0: 旧版已失效
    effective_from = Column(String(20), nullable=True)  # 生效起始日期
    effective_to = Column(String(20), nullable=True)  # 失效日期
    meta_info = Column(JSON, default=dict)  # 额外元数据
    
    created_at = Column(UTCDateTime, default=utcnow)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow)
    
    def __repr__(self):
        return f"<KnowledgeItem(id='{self.id}', crop='{self.crop}', topic='{self.topic}', category='{self.category}')>"
