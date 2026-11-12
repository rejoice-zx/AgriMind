# -*- coding: utf-8 -*-
"""
AgriMind 农业 Agent 数据契约与 Schema 定义
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Input length limits (P2-INPUT / DS-23).
# These are RESOURCE bounds with a single, consistent rejection path (pydantic
# -> HTTP 422), not a prompt-injection defence; injection risk is tracked
# separately by V-PROMPT. Every entry point uses the same constants.
# ---------------------------------------------------------------------------
MAX_QUERY_LENGTH = 2000
MAX_TOOL_NAME_LENGTH = 64
MAX_TOOL_ARGUMENTS_JSON = 8000


class ToolParameterSchema(BaseModel):
    """工具参数模式描述"""
    type: str
    description: str
    required: bool = False
    default: Optional[Any] = None
    enum: Optional[List[str]] = None


class ToolDefinition(BaseModel):
    """工具定义元数据（符合标准 Function Calling 规范）"""
    name: str
    description: str
    parameters: Dict[str, Any]
    category: str = "agriculture"


class ToolExecuteRequest(BaseModel):
    """单工具直接调用请求"""
    tool_name: str = Field(..., min_length=1, max_length=MAX_TOOL_NAME_LENGTH, description="工具名称")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="调用参数")
    field_id: Optional[int] = Field(None, description="关联田块ID（可选）")

    @field_validator("arguments")
    @classmethod
    def _bound_arguments_size(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        """Reject oversized single-step tool payloads (P2-INPUT / DS-23)."""
        import json

        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raise ValueError("arguments 无法序列化")
        if len(encoded) > MAX_TOOL_ARGUMENTS_JSON:
            raise ValueError(
                f"arguments 过大：序列化后 {len(encoded)} 字符，上限 {MAX_TOOL_ARGUMENTS_JSON}"
            )
        return value


class ToolExecuteResponse(BaseModel):
    """单工具调用结果"""
    tool_name: str
    status: str  # success / error
    result: Any
    error: Optional[str] = None


class AgentDecisionRequest(BaseModel):
    """农业 Agent 智能决策请求"""
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH, description="农户或农技员咨询提问，例如：'明天可以给1号棚番茄喷洒农药防灰霉病吗？'")
    field_id: Optional[int] = Field(None, description="指定田块ID（提供则自动注入地块时空上下文）")
    crop_planting_id: Optional[int] = Field(None, description="指定作物种植批次ID（可选）")
    image_url: Optional[str] = Field(None, max_length=2048, description="病害叶片/植株图片URL或路径（可选）")
    location: Optional[str] = Field(None, max_length=200, description="地理位置或地区（如'山东省寿光市'）")
    enable_multimodal: bool = Field(True, description="若提供图片是否启用图像初诊分析")
    conversation_id: Optional[UUID] = None
    request_id: Optional[UUID] = None

    @field_validator("query")
    @classmethod
    def nonempty_query(cls, value):
        if not value.strip():
            raise ValueError("提问不能为空")
        return value


class ToolTrace(BaseModel):
    """Agent 执行过程中工具调用的审计记录"""
    tool_name: str
    arguments: Dict[str, Any]
    result_summary: Any
    status: str = "success"
    duration_ms: float = Field(0.0, description="工具执行物理耗时(毫秒)")
    execution_metadata: Dict[str, Any] = Field(default_factory=dict, description="本次实际采用的非敏感参数与候选范围")


class AgentDecisionResponse(BaseModel):
    """农业 Agent 结构化决策输出"""
    query: str
    conclusion: str = Field(..., description="核心研判结论与裁决")
    decision_type: str = Field(..., description="决策类型：SPRAY_DECISION / FERTILIZATION / DIAGNOSIS / GDD_PREDICTION / GENERAL")
    confidence: float = Field(..., description="模型自评分(0.0-1.0)，并按实际证据设上限；不是经校准的准确率")
    can_proceed: bool = Field(True, description="是否允许/建议执行此项农事操作（如遇恶劣天气或药害风险则为False）")
    risks: List[str] = Field(default_factory=list, description="潜在农事与灾害风险清单")
    recommendations: List[str] = Field(default_factory=list, description="具体可落地的操作与技术指导建议")
    evidences: List[Dict[str, Any]] = Field(default_factory=list, description="决策支撑的技术依据（国家标准、天气、历史记录）")
    tool_traces: List[ToolTrace] = Field(default_factory=list, description="Agent 调用工具的详细追踪记录")
    need_expert: bool = Field(False, description="是否需要转交农技专家复核")
    expert_reason: Optional[str] = Field(None, description="触发专家介入的原因（低置信度/高风险/疑似疑难病害等）")
    evidence_level: str = Field("none", description="依据等级：knowledge（有知识库候选）/ records_only（仅田块、天气或履历）/ none")
    total_duration_ms: float = Field(0.0, description="Agent决策链路端到端物理耗时(毫秒)")
    conversation_id: Optional[str] = None
    turn_id: Optional[str] = None
    consultation_id: Optional[str] = None

