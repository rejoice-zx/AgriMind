# -*- coding: utf-8 -*-
"""Knowledge Schemas - Pydantic models for knowledge API"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime


# ==================== 知识条目相关 ====================

class CreateKnowledgeRequest(BaseModel):
    """创建知识条目请求"""
    question: str = Field(..., min_length=1, max_length=2000, description="问题")
    answer: str = Field(..., min_length=1, max_length=10000, description="答案")
    keywords: Optional[List[str]] = Field(default=[], description="关键词列表")
    category: str = Field(default="通用", max_length=50, description="分类")
    score: float = Field(default=1.0, ge=0.0, le=10.0, description="权重分数")
    # 农业元数据
    crop: Optional[str] = Field(None, max_length=50, description="作物名称（如番茄）")
    region: Optional[str] = Field(None, max_length=50, description="适用地区（如江苏）")
    growth_stage: Optional[str] = Field(None, max_length=50, description="生长阶段（如开花期）")
    topic: Optional[str] = Field(None, max_length=50, description="主题类型（disease/pest/fertilizer等）")
    disease: Optional[str] = Field(None, max_length=100, description="病害名称")
    source_type: Optional[str] = Field("unverified", max_length=50, description="来源类型标注；缺失保持未核验")
    source_name: Optional[str] = Field(None, max_length=200, description="来源规范名称")
    authority_level: Optional[str] = Field("未核验", max_length=10, description="来源分级标注；不代表已核验")
    version: Optional[str] = Field(None, max_length=20, description="来源版本，缺失留空")
    publish_date: Optional[str] = Field(None, max_length=20, description="规范/文献发布日期")
    url: Optional[str] = Field(None, max_length=500, description="权威溯源链接")
    source_updated_at: Optional[str] = Field(None, max_length=20, description="规范修订日期")
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    is_current: Optional[Literal[0, 1]] = 1


class UpdateKnowledgeRequest(BaseModel):
    """更新知识条目请求"""
    question: Optional[str] = Field(None, min_length=1, max_length=2000)
    answer: Optional[str] = Field(None, min_length=1, max_length=10000)
    keywords: Optional[List[str]] = None
    category: Optional[str] = Field(None, max_length=50)
    score: Optional[float] = Field(None, ge=0.0, le=10.0)
    crop: Optional[str] = None
    region: Optional[str] = None
    growth_stage: Optional[str] = None
    topic: Optional[str] = None
    disease: Optional[str] = None
    source_type: Optional[str] = None
    source_name: Optional[str] = None
    authority_level: Optional[str] = None
    publish_date: Optional[str] = None
    url: Optional[str] = None
    source_updated_at: Optional[str] = None
    version: Optional[str] = None
    is_current: Optional[Literal[0, 1]] = None
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None


class KnowledgeResponse(BaseModel):
    """知识条目响应"""
    id: str
    question: str
    answer: str
    keywords: List[str] = []
    category: str = "通用"
    score: float = 1.0
    crop: Optional[str] = None
    region: Optional[str] = None
    growth_stage: Optional[str] = None
    topic: Optional[str] = None
    disease: Optional[str] = None
    source_type: Optional[str] = "unverified"
    source_name: Optional[str] = None
    authority_level: Optional[str] = "未核验"
    publish_date: Optional[str] = None
    url: Optional[str] = None
    source_updated_at: Optional[str] = None
    version: Optional[str] = None
    is_current: Optional[int] = 1
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class KnowledgeListResponse(BaseModel):
    """知识列表响应"""
    items: List[KnowledgeResponse]
    total: int
    page: int
    page_size: int


# ==================== Hybrid RAG 检索模型 ====================

class HybridSearchRequest(BaseModel):
    """Hybrid RAG 检索请求"""
    query: str = Field(..., min_length=1, max_length=2000, description="用户查询问题")
    crop: Optional[str] = Field(None, description="限定作物（如番茄）")
    growth_stage: Optional[str] = Field(None, description="限定生长期（如开花期）")
    topic: Optional[str] = Field(None, description="限定主题（disease/fertilizer等）")
    region: Optional[str] = Field(None, description="限定地区（如江苏）")
    top_k: Optional[int] = Field(default=None, ge=1, le=20, description="省略采用设置值，显式值受设置上限约束")
    min_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False, description="兼容字段：管理员诊断时可覆盖最低融合得分，省略采用设置值")


class HybridSearchItem(BaseModel):
    """混合检索命中项"""
    id: str
    question: str
    answer: str
    score: float
    vector_score: float = 0.0
    bm25_score: float = 0.0
    authority_level: str = "未核验"
    source_name: Optional[str] = None
    publish_date: Optional[str] = None
    url: Optional[str] = None
    crop: Optional[str] = None
    growth_stage: Optional[str] = None
    disease: Optional[str] = None


class HybridSearchResponse(BaseModel):
    """Hybrid RAG 检索诊断响应"""
    query: str
    rewritten_query: str
    intent: str
    extracted_entities: dict
    retrieved_items: List[HybridSearchItem]
    context_text: str
    confidence: float
    need_expert: bool
    expert_reason: Optional[str] = None
    retrieval_trace: dict = Field(default_factory=dict)


# ==================== 导入导出相关 ====================

class ImportKnowledgeItem(CreateKnowledgeRequest):
    """Preserve source metadata; source IDs/timestamps are informational, never an overwrite target."""
    id: Optional[str] = Field(None, max_length=50)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    is_current: Optional[Literal[0, 1]] = 1
    meta_info: Optional[Dict[str, Any]] = None


class ImportKnowledgeRequest(BaseModel):
    """批量导入请求"""
    items: List[ImportKnowledgeItem]
    skip_duplicates: bool = Field(default=True, description="是否跳过重复项")


class ImportKnowledgeResponse(BaseModel):
    """批量导入响应"""
    success_count: int
    skip_count: int
    errors: List[str]


class ExportKnowledgeResponse(BaseModel):
    """导出响应"""
    items: List[dict]
    total: int


# ==================== 索引管理相关 ====================

class IndexStatusResponse(BaseModel):
    """索引状态响应"""
    count: int
    dimension: int
    embedding_model: Optional[str] = None
    embedding_available: bool
    embedding_dimension: Optional[int] = None
    needs_rebuild: bool = False
    rebuild_reason: Optional[str] = None
    index_generation: Optional[str] = None
    index_contract: Optional[dict] = None
    current_contract: Optional[dict] = None
    vector_compatible: bool = False


class RebuildIndexResponse(BaseModel):
    """重建索引响应"""
    success: bool
    success_count: int
    errors: List[str]
    message: str
