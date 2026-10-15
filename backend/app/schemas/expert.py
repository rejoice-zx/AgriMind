# -*- coding: utf-8 -*-
"""
AgriMind 专家工作台与多模态诊断数据契约 (Expert Workbench Schemas)
"""

from datetime import datetime
from uuid import UUID
from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

# 输入长度上限（P2-INPUT / DS-23）：资源边界，统一由 pydantic 产生 HTTP 422；
# 与 schemas/agent.py 的上限语义一致，不是 prompt 注入防护（那属 V-PROMPT）。
MAX_IMAGE_URL_LENGTH = 2048
MAX_SYMPTOMS_HINT_LENGTH = 1000
MAX_CROP_NAME_LENGTH = 64


class VisualDiagnosisRequest(BaseModel):
    """多模态图像诊断请求"""
    request_id: Optional[UUID] = None
    image_url: str = Field(..., min_length=1, max_length=MAX_IMAGE_URL_LENGTH, description="病斑图片URL或文件名")
    field_id: Optional[int] = Field(None, description="关联田块ID（提供则自动读取时空档案与气象）")
    crop_planting_id: Optional[int] = Field(None, ge=1, description="明确关联本田块的在种批次，多批次时必填")
    crop: Optional[str] = Field(None, max_length=MAX_CROP_NAME_LENGTH, description="作物名称，缺失时不代填")
    symptoms_hint: Optional[str] = Field(None, max_length=MAX_SYMPTOMS_HINT_LENGTH, description="视觉特征补充描述（如'边缘不规则黄褐色轮纹'）")
    auto_create_ticket: bool = Field(True, description="低置信度是否自动派单；视觉工具明确要求专家复核时仍强制派单，关闭不取消诊断记录及复核提示")

    @model_validator(mode='after')
    def batch_requires_field(self):
        if self.crop_planting_id is not None and self.field_id is None:
            raise ValueError('选择作物批次时必须关联田块')
        return self


class VisualDiagnosisResponse(BaseModel):
    """多模态图像诊断结果"""
    diagnosis_status: Literal["completed", "failed"] = "completed"
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    request_id: Optional[str] = None
    replayed: bool = False
    processed_at: Optional[datetime] = None
    image_url: str
    crop: str
    suspected_disease: str
    confidence: Optional[float] = Field(None, ge=0, le=1, allow_inf_nan=False)
    features_detected: List[str] = Field(default_factory=list)
    severity: str = "unknown"
    need_expert: bool
    disease_record_id: Optional[int] = None
    ticket_id: Optional[int] = None
    advice: str
    standard_treatment: Optional[str] = None


class DiagnosisRequestStatus(BaseModel):
    request_id: str
    status: str
    result: Optional[VisualDiagnosisResponse] = None
    message: str


class ExpertFeedback(BaseModel):
    confirmed_disease: Optional[str] = None
    treatment_suggestion: str
    notes: Optional[str] = None
    expert_case_id: Optional[int] = None
    expert_name: Optional[str] = None
    resolved_at: Optional[datetime] = None
    workflow_verified: bool = False


class ExpertTicketItem(BaseModel):
    """专家工作台待复核工单项"""
    ticket_id: int
    disease_record_id: int
    field_id: int
    field_name: Optional[str] = None
    farm_name: Optional[str] = None
    region: Optional[str] = None
    crop: str
    variety: Optional[str] = None
    growth_stage: str
    image_url: Optional[str] = None
    initial_suspected_disease: str
    initial_confidence: Optional[float] = None
    symptoms: Optional[str] = None
    status: str = Field("pending_review", description="pending_review (待接单) / in_review (审阅中) / expert_reviewed (已确诊)")
    expert_id: Optional[int] = None
    expert_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    claim_available: bool = False
    feedback: Optional[ExpertFeedback] = None
    diagnosis_method: str = "unknown"


class ExpertTicketDetail(ExpertTicketItem):
    """工单全维度上下文详情（供专家诊断）"""
    field_soil_type: Optional[str] = None
    field_area: Optional[float] = None
    current_weather: Optional[Dict[str, Any]] = None
    recent_farming_records: List[Dict[str, Any]] = Field(default_factory=list)
    ai_features: List[str] = Field(default_factory=list)


class ExpertTicketListResponse(BaseModel):
    """工单列表响应"""
    total: int
    items: List[ExpertTicketItem]


class ExpertResolveRequest(BaseModel):
    """专家确诊与处方提交"""
    confirmed_disease: str = Field(..., min_length=1, max_length=100, description="专家最终确诊病害全称")
    treatment_suggestion: str = Field(..., min_length=5, max_length=6000, description="经复核的建议，资料不足时说明补充内容")
    notes: Optional[str] = Field(None, max_length=4000, description="诊断手记、典型症状要点说明")
    is_knowledge_candidate: int = Field(0, ge=0, le=1, description="专家明确勾选后才沉淀为 L4 级案例 (1:是, 0:否)")

    @field_validator("confirmed_disease", "treatment_suggestion")
    @classmethod
    def meaningful_text(cls, value, info):
        value = value.strip()
        minimum = 5 if info.field_name == "treatment_suggestion" else 1
        if len(value) < minimum:
            raise ValueError("复核内容不能为空或仅含空白")
        return value

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value):
        return value.strip() or None if value is not None else None


class ExpertResolveResponse(BaseModel):
    """专家确诊出具结果"""
    ticket_id: int
    status: str
    confirmed_disease: str
    treatment_suggestion: str
    expert_case_id: int
    knowledge_item_id: Optional[str] = None
    feedback_message: str



class ImageUploadResponse(BaseModel):
    """图片上传结果"""
    filename: str
    url: str
    size: int
    content_type: str
