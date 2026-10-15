# -*- coding: utf-8 -*-
"""Settings API Schemas"""
from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Literal
from app.schemas.custom_weather import CustomWeatherConfig


class LLMConfigRequest(BaseModel):
    """LLM配置请求"""
    provider: str = Field(description="提供商名称")
    api_key: Optional[str] = Field(default=None, description="API密钥")
    api_url: Optional[str] = Field(default=None, description="API地址")
    model: Optional[str] = Field(default=None, description="模型名称")


class LLMConfigResponse(BaseModel):
    """LLM配置响应"""
    provider: str = Field(description="提供商名称")
    api_key_masked: str = Field(description="遮蔽的API密钥")
    api_url: str = Field(description="API地址")
    model: str = Field(description="模型名称")
    has_api_key: bool = Field(description="是否已配置API密钥")


class RAGConfigRequest(BaseModel):
    """RAG配置请求"""
    top_k: int = Field(default=5, ge=1, le=20, description="检索数量")
    similarity_threshold: float = Field(
        default=0.5, 
        ge=0.0, 
        le=1.0, 
        allow_inf_nan=False,
        description="农业检索最低融合得分，非正确率"
    )
    use_rewrite: bool = Field(default=True, description="是否使用查询改写")
    max_context_length: int = Field(
        default=2000, 
        ge=500, 
        le=10000, 
        description="最大上下文长度"
    )
    chunk_size: int = Field(default=500, ge=100, le=5000, description="分块大小")
    chunk_overlap: int = Field(default=50, ge=0, le=1000, description="分块重叠大小")

    @model_validator(mode="after")
    def chunk_geometry(self):
        if {"chunk_size", "chunk_overlap"} <= self.model_fields_set and self.chunk_overlap >= self.chunk_size:
            raise ValueError("分块重叠必须小于分块大小")
        return self


class RAGConfigResponse(BaseModel):
    """RAG配置响应"""
    top_k: int = Field(description="检索数量")
    similarity_threshold: float = Field(description="相似度阈值")
    use_rewrite: bool = Field(description="是否使用查询改写")
    max_context_length: int = Field(description="最大上下文长度")
    chunk_size: int = Field(default=500, description="分块大小")
    chunk_overlap: int = Field(default=50, description="分块重叠大小")


class EmbeddingConfigRequest(BaseModel):
    """Embedding配置请求"""
    provider: Optional[str] = Field(default=None, description="提供商名称")
    api_key: Optional[str] = Field(default=None, description="API密钥（为空则使用LLM的）")
    api_url: Optional[str] = Field(default=None, description="API地址")
    model: Optional[str] = Field(default=None, description="模型名称")
    dimension: Optional[int] = Field(default=None, ge=64, le=4096, description="预期向量维度")
    request_dimensions: Optional[bool] = None


class EmbeddingConfigResponse(BaseModel):
    """Embedding配置响应"""
    provider: str = Field(description="提供商名称")
    api_key_masked: str = Field(description="遮蔽的API密钥")
    api_url: str = Field(description="API地址")
    model: str = Field(description="模型名称")
    dimension: int = Field(description="向量维度")
    request_dimensions: bool = False
    credential_source: str = "missing"
    config_origin: str = "saved"
    has_api_key: bool = Field(description="是否已配置API密钥")


class AgriAIConfigRequest(BaseModel):
    """农业AI、多模态视觉与气象服务配置请求"""
    llm_api_key: Optional[str] = Field(default=None, description="大模型API密钥")
    llm_base_url: Optional[str] = Field(default=None, description="大模型Base URL")
    llm_model: Optional[str] = Field(default=None, description="决策大模型名称")
    vision_api_key: Optional[str] = Field(default=None, description="多模态视觉API密钥")
    vision_base_url: Optional[str] = Field(default=None, description="视觉Base URL")
    vision_model: Optional[str] = Field(default=None, description="视觉模型名称")
    weather_provider: Optional[Literal["open_meteo", "custom"]] = Field(default=None, description="气象服务提供商")
    weather_custom: Optional[CustomWeatherConfig] = None
    weather_api_key: Optional[str] = Field(default=None, description="气象API密钥")


class AgriAIConfigResponse(BaseModel):
    """农业AI、多模态视觉与气象服务配置响应"""
    llm_api_key_masked: str = Field(default="", description="遮蔽的LLM密钥")
    llm_base_url: str = Field(default="https://api.siliconflow.cn/v1")
    llm_model: str = Field(default="deepseek-ai/DeepSeek-V3")
    has_llm_key: bool = Field(default=False)
    vision_api_key_masked: str = Field(default="", description="遮蔽的视觉密钥")
    vision_base_url: str = Field(default="https://api.siliconflow.cn/v1")
    vision_model: str = Field(default="Qwen/Qwen3-VL-8B-Instruct")
    has_vision_key: bool = Field(default=False)
    weather_provider: str = Field(default="open_meteo")
    weather_custom: CustomWeatherConfig = Field(default_factory=CustomWeatherConfig)
    weather_api_key_masked: str = Field(default="", description="遮蔽的天气密钥")
    has_weather_key: bool = Field(default=False)


class SettingsResponse(BaseModel):
    """设置响应"""
    llm: LLMConfigResponse = Field(description="LLM配置")
    embedding: EmbeddingConfigResponse = Field(description="Embedding配置")
    rag: RAGConfigResponse = Field(description="RAG配置")
    general: Optional['GeneralConfigResponse'] = Field(default=None, description="通用配置")
    agri_ai: Optional[AgriAIConfigResponse] = Field(default=None, description="农业AI与外部服务配置")


class GeneralConfigRequest(BaseModel):
    """通用配置请求"""
    timezone: Optional[str] = Field(default=None, description="时区，如 Asia/Shanghai")


class GeneralConfigResponse(BaseModel):
    """通用配置响应"""
    timezone: str = Field(default="Asia/Shanghai", description="时区")


class SettingsUpdateRequest(BaseModel):
    """设置更新请求"""
    llm: Optional[LLMConfigRequest] = Field(default=None, description="LLM配置")
    embedding: Optional[EmbeddingConfigRequest] = Field(default=None, description="Embedding配置")
    rag: Optional[RAGConfigRequest] = Field(default=None, description="RAG配置")
    general: Optional[GeneralConfigRequest] = Field(default=None, description="通用配置")
    agri_ai: Optional[AgriAIConfigRequest] = Field(default=None, description="农业AI与外部服务配置")


class LLMProviderInfo(BaseModel):
    """LLM提供商信息"""
    name: str = Field(description="提供商标识")
    display_name: str = Field(description="显示名称")
    default_api_url: str = Field(description="默认API地址")
    default_model: str = Field(description="默认模型")
    supported_models: List[str] = Field(description="支持的模型列表")


class LLMProvidersResponse(BaseModel):
    """LLM提供商列表响应"""
    providers: List[LLMProviderInfo] = Field(description="提供商列表")


class TestConnectionRequest(BaseModel):
    """测试连接请求"""
    provider: str = Field(description="提供商名称")
    api_key: Optional[str] = Field(default=None, description="API密钥（可选，不传则使用已保存的）")
    api_url: Optional[str] = Field(default=None, description="API地址")
    model: Optional[str] = Field(default=None, description="模型名称")


class TestConnectionResponse(BaseModel):
    """测试连接响应"""
    success: bool = Field(description="是否成功")
    message: str = Field(description="消息")


class TestVisionRequest(BaseModel):
    """测试视觉模型连接请求"""
    api_key: Optional[str] = Field(default=None, description="API密钥")
    base_url: Optional[str] = Field(default=None, description="Base URL")
    model: Optional[str] = Field(default=None, description="模型名称")


class TestWeatherRequest(BaseModel):
    """测试气象服务连接请求"""
    provider: Literal["open_meteo", "custom"] = Field(default="open_meteo", description="提供商")
    weather_custom: Optional[CustomWeatherConfig] = None
    latitude: Optional[float] = Field(None, ge=-90, le=90, strict=True, allow_inf_nan=False)
    longitude: Optional[float] = Field(None, ge=-180, le=180, strict=True, allow_inf_nan=False)
    api_key: Optional[str] = Field(default=None, description="API密钥")


class ModelProbeRequest(BaseModel):
    kind: Literal['decision', 'vision']
    api_key: Optional[str] = None
    base_url: Optional[str] = Field(None, max_length=2048)
    model: Optional[str] = Field(None, max_length=200)


class ModelCapability(BaseModel):
    id: str
    image_input: Literal['supported', 'unsupported', 'unknown']
    evidence: Literal['metadata', 'model_name', 'unspecified']
    vision_candidate: bool
    message: str


class ModelDiscoveryResponse(BaseModel):
    success: bool
    message: str
    models: List[str] = Field(default_factory=list)
    model_details: List[ModelCapability] = Field(default_factory=list)
    truncated: bool = False
    source: Literal['live_api'] = 'live_api'
    code: str
    request_id: Optional[str] = None


class ModelCheck(BaseModel):
    name: str
    state: Literal['passed', 'failed', 'unverified']
    code: str
    message: str
    upstream_trace_id: Optional[str] = None


class ModelProbeResponse(BaseModel):
    success: bool
    message: str
    checks: List[ModelCheck] = Field(default_factory=list)
    authentication: Literal['unknown', 'verified', 'failed'] = 'unknown'
    vision: Literal['not_tested', 'sample_verified', 'accepted_unverified', 'failed'] = 'not_tested'
    request_id: Optional[str] = None

