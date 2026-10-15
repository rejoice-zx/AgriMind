# -*- coding: utf-8 -*-
"""Settings Management API Endpoints"""
import logging
from dataclasses import asdict
from pydantic import ValidationError

from fastapi import APIRouter, Depends, HTTPException, Response, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.dependencies import require_admin
from app.models.user import User
from app.services.settings_service import (
    SettingsService,
    LLMConfig,
    EmbeddingConfig,
    RAGConfig,
    GeneralConfig,
    AgriAIConfig,
    SettingsEncryptionError, SettingsCipherUnavailable,
)
from app.schemas.settings import (
    SettingsResponse,
    RAGConfigRequest,
    SettingsUpdateRequest,
    LLMConfigResponse,
    EmbeddingConfigResponse,
    RAGConfigResponse,
    GeneralConfigResponse,
    AgriAIConfigResponse,
    LLMProvidersResponse,
    LLMProviderInfo,
    TestConnectionRequest,
    TestConnectionResponse,
    TestVisionRequest,
    TestWeatherRequest,
    ModelProbeRequest, ModelProbeResponse, ModelDiscoveryResponse,
)

from app.core.audit_logging import audit
from app.services.model_probe_service import ModelProbeService
from app.core.llm_providers import get_provider
from app.schemas.custom_weather import CustomWeatherConfig, credential_identity

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/settings", tags=["settings"])


async def settings_db(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        yield db
    except (SettingsEncryptionError, SettingsCipherUnavailable) as error:
        await db.rollback()
        unavailable = isinstance(error, SettingsCipherUnavailable)
        message = ("配置密文无法读取，请恢复原配置加密密钥；不要随意更换密钥。" if unavailable else
                   "未配置可用的独立配置加密密钥（至少32字符），本次未保存。请使用本地启动器初始化或由管理员配置 SETTINGS_ENCRYPTION_KEY 后重启。")
        headers = {"X-Error-Code": "SETTINGS_CIPHER_UNAVAILABLE" if unavailable else "SETTINGS_ENCRYPTION_UNAVAILABLE"}
        request_id = getattr(request.state, "request_id", None)
        if request_id: headers["X-Request-ID"] = request_id
        raise HTTPException(503, message, headers=headers) from error


@router.get('/security-status')
async def settings_security_status(response: Response, current_user: User = Depends(require_admin())):
    from app.config import settings
    response.headers['Cache-Control'] = 'private, no-store'
    ready = len(settings.SETTINGS_ENCRYPTION_KEY.strip()) >= 32
    return {"encryption_ready": ready, "message": "配置加密已就绪" if ready else
            "配置加密未就绪。请运行 backend/run.py 或配置独立 SETTINGS_ENCRYPTION_KEY 后重启；已有密文须使用原密钥。"}


def model_for_address(saved_url, saved_model, new_url, submitted):
    same_address = saved_url.strip().rstrip('/') == new_url.strip().rstrip('/')
    if submitted is None and same_address:
        return saved_model
    if not submitted or not submitted.strip():
        raise HTTPException(422, '请明确选择或手填模型；切换地址不沿用旧模型，本次未保存')
    return submitted.strip()


@router.post('/model-discovery', response_model=ModelDiscoveryResponse)
async def discover_agri_models(request: ModelProbeRequest, response: Response, http_request: Request,
    db: AsyncSession = Depends(settings_db), current_user: User = Depends(require_admin())):
    response.headers['Cache-Control'] = 'private, no-store'
    config = await SettingsService.resolve_agri_model(db, request.kind, request.api_key, request.base_url, request.model)
    result = await ModelProbeService.discover(config)
    result['request_id'] = getattr(http_request.state, 'request_id', None)
    audit('model_list_returned', actor_id=current_user.id)
    return result


@router.post('/model-test', response_model=ModelProbeResponse)
async def test_agri_model(request: ModelProbeRequest, response: Response, http_request: Request,
    db: AsyncSession = Depends(settings_db), current_user: User = Depends(require_admin())):
    response.headers['Cache-Control'] = 'private, no-store'
    config = await SettingsService.resolve_agri_model(db, request.kind, request.api_key, request.base_url, request.model)
    result = await ModelProbeService.test(config, request.kind)
    result['request_id'] = getattr(http_request.state, 'request_id', None)
    last_check = result['checks'][-1] if result['checks'] else {}
    audit('model_test_passed' if result['success'] else 'model_test_failed', actor_id=current_user.id,
          probe_stage=last_check.get('name'), probe_code=last_check.get('code'))
    return result


@router.get("", response_model=SettingsResponse)
async def get_settings(
    db: AsyncSession = Depends(settings_db),
    current_user: User = Depends(require_admin())
):
    """
    获取系统设置
    
    需要管理员权限
    
    返回:
    - LLM配置（API密钥已遮蔽）
    - Embedding配置
    - RAG配置
    - 通用配置
    - 农业AI与气象配置
    """
    config = await SettingsService.get_all_settings(db)
    effective_embedding = await SettingsService.get_embedding_config_with_env(db)
    
    return SettingsResponse(
        llm=LLMConfigResponse(
            provider=config.llm.provider,
            api_key_masked=SettingsService.mask_api_key(config.llm.api_key),
            api_url=config.llm.api_url,
            model=config.llm.model,
            has_api_key=bool(config.llm.api_key),
        ),
        embedding=EmbeddingConfigResponse(
            provider=effective_embedding.provider,
            api_key_masked=SettingsService.mask_api_key(effective_embedding.api_key),
            api_url=effective_embedding.api_url,
            model=effective_embedding.model,
            dimension=effective_embedding.dimension,
            request_dimensions=effective_embedding.request_dimensions,
            credential_source=getattr(effective_embedding, "credential_source", "missing"),
            config_origin=getattr(effective_embedding, "config_origin", "saved"),
            has_api_key=bool(effective_embedding.api_key),
        ),
        rag=RAGConfigResponse(
            top_k=config.rag.top_k,
            similarity_threshold=config.rag.similarity_threshold,
            use_rewrite=config.rag.use_rewrite,
            max_context_length=config.rag.max_context_length,
            chunk_size=config.rag.chunk_size,
            chunk_overlap=config.rag.chunk_overlap,
        ),
        general=GeneralConfigResponse(
            timezone=config.general.timezone,
        ),
        agri_ai=AgriAIConfigResponse(
            llm_api_key_masked=SettingsService.mask_api_key(config.agri_ai.llm_api_key),
            llm_base_url=config.agri_ai.llm_base_url,
            llm_model=config.agri_ai.llm_model,
            has_llm_key=bool(config.agri_ai.llm_api_key),
            vision_api_key_masked=SettingsService.mask_api_key(config.agri_ai.vision_api_key),
            vision_base_url=config.agri_ai.vision_base_url,
            vision_model=config.agri_ai.vision_model,
            has_vision_key=bool(config.agri_ai.vision_api_key),
            weather_provider=config.agri_ai.weather_provider,
            weather_custom=CustomWeatherConfig.model_validate(config.agri_ai.weather_custom),
            weather_api_key_masked=SettingsService.mask_api_key(config.agri_ai.weather_api_key),
            has_weather_key=bool(config.agri_ai.weather_api_key),
        ),
    )



@router.put("", response_model=SettingsResponse)
async def update_settings(
    request: SettingsUpdateRequest,
    db: AsyncSession = Depends(settings_db),
    current_user: User = Depends(require_admin())
):
    """
    更新系统设置
    
    需要管理员权限
    
    参数:
    - llm: LLM配置（可选）
    - embedding: Embedding配置（可选）
    - rag: RAG配置（可选）
    
    返回:
    - 更新后的设置
    """
    # 获取当前配置
    current_config = await SettingsService.get_all_settings(db)
    
    # 更新LLM配置
    llm_config = None
    if request.llm:
        try:
            target_url = request.llm.api_url or get_provider(request.llm.provider).default_api_url
            saved_url = current_config.llm.api_url or get_provider(current_config.llm.provider).default_api_url
        except ValueError:
            raise HTTPException(422, "未知模型提供商，本次未保存") from None
        llm_config = LLMConfig(
            provider=request.llm.provider,
            api_key=SettingsService.endpoint_key(saved_url, current_config.llm.api_key, target_url, request.llm.api_key or None),
            api_url=request.llm.api_url or "",
            model=request.llm.model or "",
        )
    
    # 更新Embedding配置
    embedding_config = None
    if request.embedding:
        effective = await SettingsService.get_embedding_config_with_env(db)
        target_provider = request.embedding.provider or effective.provider
        target_url = (request.embedding.api_url or get_provider(target_provider).default_api_url) if request.embedding.api_url is not None else (
            get_provider(target_provider).default_api_url if target_provider != effective.provider else effective.api_url)
        embedding_config = EmbeddingConfig(provider=target_provider,
            api_key=SettingsService.endpoint_key(effective.api_url, current_config.embedding.api_key, target_url, request.embedding.api_key),
            api_url=target_url,
            model=model_for_address(effective.api_url, effective.model, target_url, request.embedding.model),
            dimension=request.embedding.dimension or effective.dimension,
            request_dimensions=request.embedding.request_dimensions if request.embedding.request_dimensions is not None else effective.request_dimensions)

    # 更新RAG配置
    rag_config = None
    if request.rag:
        merged = {**asdict(current_config.rag), **request.rag.model_dump(exclude_unset=True)}
        try:
            validated = RAGConfigRequest.model_validate(merged, strict=True)
        except ValidationError:
            raise HTTPException(422, "RAG参数无效，分块重叠必须小于分块大小；本次未保存任何设置") from None
        rag_config = RAGConfig(**validated.model_dump())

    # 更新通用配置
    general_config = None
    if request.general:
        general_config = GeneralConfig(
            timezone=request.general.timezone or current_config.general.timezone,
        )

    # 更新农业AI配置
    agri_ai_config = None
    if request.agri_ai:
        weather_provider = request.agri_ai.weather_provider or current_config.agri_ai.weather_provider
        custom_weather = (request.agri_ai.weather_custom.model_dump() if request.agri_ai.weather_custom is not None
                          else current_config.agri_ai.weather_custom)
        if weather_provider == 'custom' and not CustomWeatherConfig.model_validate(custom_weather).endpoint:
            raise HTTPException(status_code=422, detail='请填写自定义天气完整HTTPS接口地址')
        old_scope = credential_identity(current_config.agri_ai.weather_provider, current_config.agri_ai.weather_custom)
        new_scope = credential_identity(weather_provider, custom_weather)
        weather_key = request.agri_ai.weather_api_key if request.agri_ai.weather_api_key is not None else (
            current_config.agri_ai.weather_api_key if old_scope == new_scope else '')
        agri_ai_config = AgriAIConfig(
            llm_api_key=SettingsService.endpoint_key(current_config.agri_ai.llm_base_url, current_config.agri_ai.llm_api_key,
                request.agri_ai.llm_base_url or current_config.agri_ai.llm_base_url, request.agri_ai.llm_api_key),
            llm_base_url=request.agri_ai.llm_base_url or current_config.agri_ai.llm_base_url,
            llm_model=model_for_address(current_config.agri_ai.llm_base_url, current_config.agri_ai.llm_model,
                request.agri_ai.llm_base_url or current_config.agri_ai.llm_base_url, request.agri_ai.llm_model),
            vision_api_key=SettingsService.endpoint_key(current_config.agri_ai.vision_base_url, current_config.agri_ai.vision_api_key,
                request.agri_ai.vision_base_url or current_config.agri_ai.vision_base_url, request.agri_ai.vision_api_key),
            vision_base_url=request.agri_ai.vision_base_url or current_config.agri_ai.vision_base_url,
            vision_model=model_for_address(current_config.agri_ai.vision_base_url, current_config.agri_ai.vision_model,
                request.agri_ai.vision_base_url or current_config.agri_ai.vision_base_url, request.agri_ai.vision_model),
            weather_provider=weather_provider, weather_custom=custom_weather,
            weather_api_key=weather_key,
        )
    
    # 保存更新
    await SettingsService.update_settings(
        db, 
        llm_config=llm_config, 
        embedding_config=embedding_config, 
        rag_config=rag_config, 
        general_config=general_config,
        agri_ai_config=agri_ai_config
    )

    # ── 审计日志：记录配置变更（不记录 API Key 明文） ──
    changed_sections: list[str] = []
    if llm_config:
        changed_sections.append(
            f"LLM(provider={llm_config.provider}, model={llm_config.model}, "
            f"api_url={llm_config.api_url}, api_key_changed={bool(request.llm and request.llm.api_key)})"
        )
    if embedding_config:
        changed_sections.append(
            f"Embedding(provider={embedding_config.provider}, model={embedding_config.model}, "
            f"dimension={embedding_config.dimension}, "
            f"api_key_changed={bool(request.embedding and request.embedding.api_key)})"
        )
    if rag_config:
        changed_sections.append(
            f"RAG(top_k={rag_config.top_k}, threshold={rag_config.similarity_threshold}, "
            f"rewrite={rag_config.use_rewrite}, max_ctx={rag_config.max_context_length}, "
            f"chunk={rag_config.chunk_size}/{rag_config.chunk_overlap})"
        )
    if general_config:
        changed_sections.append(f"General(timezone={general_config.timezone})")
    if agri_ai_config:
        changed_sections.append(
            f"AgriAI(llm_model={agri_ai_config.llm_model}, "
            f"vision_model={agri_ai_config.vision_model}, "
            f"weather_provider={agri_ai_config.weather_provider})"
        )
    if changed_sections:
        logger.info(
            "系统设置变更 | operator=%s (id=%d) | %s",
            current_user.username, current_user.id, " | ".join(changed_sections),
        )
    
    # 时区变更时同步到日志格式化器
    if general_config and general_config.timezone:
        from app.main import get_log_formatter
        get_log_formatter().set_timezone(general_config.timezone)

    # 同步 chunk 配置到运行时 Config，使 knowledge_service 立即生效
    if rag_config:
        from app.core.config import Config as RuntimeConfig
        RuntimeConfig().apply_rag_config(rag_config)
    
    # 返回更新后的配置
    updated_config = await SettingsService.get_all_settings(db)
    effective_embedding = await SettingsService.get_embedding_config_with_env(db)
    
    return SettingsResponse(
        llm=LLMConfigResponse(
            provider=updated_config.llm.provider,
            api_key_masked=SettingsService.mask_api_key(updated_config.llm.api_key),
            api_url=updated_config.llm.api_url,
            model=updated_config.llm.model,
            has_api_key=bool(updated_config.llm.api_key),
        ),
        embedding=EmbeddingConfigResponse(
            provider=effective_embedding.provider,
            api_key_masked=SettingsService.mask_api_key(effective_embedding.api_key),
            api_url=effective_embedding.api_url,
            model=effective_embedding.model,
            dimension=effective_embedding.dimension,
            request_dimensions=effective_embedding.request_dimensions,
            credential_source=getattr(effective_embedding, "credential_source", "missing"),
            config_origin=getattr(effective_embedding, "config_origin", "saved"),
            has_api_key=bool(effective_embedding.api_key),
        ),
        rag=RAGConfigResponse(
            top_k=updated_config.rag.top_k,
            similarity_threshold=updated_config.rag.similarity_threshold,
            use_rewrite=updated_config.rag.use_rewrite,
            max_context_length=updated_config.rag.max_context_length,
            chunk_size=updated_config.rag.chunk_size,
            chunk_overlap=updated_config.rag.chunk_overlap,
        ),
        general=GeneralConfigResponse(
            timezone=updated_config.general.timezone,
        ),
        agri_ai=AgriAIConfigResponse(
            llm_api_key_masked=SettingsService.mask_api_key(updated_config.agri_ai.llm_api_key),
            llm_base_url=updated_config.agri_ai.llm_base_url,
            llm_model=updated_config.agri_ai.llm_model,
            has_llm_key=bool(updated_config.agri_ai.llm_api_key),
            vision_api_key_masked=SettingsService.mask_api_key(updated_config.agri_ai.vision_api_key),
            vision_base_url=updated_config.agri_ai.vision_base_url,
            vision_model=updated_config.agri_ai.vision_model,
            has_vision_key=bool(updated_config.agri_ai.vision_api_key),
            weather_provider=updated_config.agri_ai.weather_provider,
            weather_custom=CustomWeatherConfig.model_validate(updated_config.agri_ai.weather_custom),
            weather_api_key_masked=SettingsService.mask_api_key(updated_config.agri_ai.weather_api_key),
            has_weather_key=bool(updated_config.agri_ai.weather_api_key),
        ),
    )


@router.get("/llm-providers", response_model=LLMProvidersResponse)
async def get_llm_providers(
    current_user: User = Depends(require_admin())
):
    """
    获取LLM提供商列表
    
    需要管理员权限
    
    返回:
    - 支持的LLM提供商列表（名称、默认API地址、支持的模型）
    """
    providers = SettingsService.get_llm_providers()
    
    return LLMProvidersResponse(
        providers=[
            LLMProviderInfo(
                name=p["name"],
                display_name=p["display_name"],
                default_api_url=p["default_api_url"],
                default_model=p["default_model"],
                supported_models=p["supported_models"],
            )
            for p in providers
        ]
    )


@router.post("/test-connection", response_model=TestConnectionResponse)
async def test_llm_connection(
    request: TestConnectionRequest,
    db: AsyncSession = Depends(settings_db),
    current_user: User = Depends(require_admin())
):
    """
    测试LLM API连接
    
    需要管理员权限
    """
    current_config = await SettingsService.get_llm_config(db)
    try:
        target_url = request.api_url or get_provider(request.provider).default_api_url
        saved_url = current_config.api_url or get_provider(current_config.provider).default_api_url
    except ValueError:
        return TestConnectionResponse(success=False, message="未知模型提供商")
    api_key = SettingsService.endpoint_key(saved_url, current_config.api_key, target_url, request.api_key or None)
    if not api_key:
        return TestConnectionResponse(success=False, message="此地址没有可用密钥，请输入该平台密钥")

    success, message = await SettingsService.test_llm_connection(
        provider=request.provider,
        api_key=api_key,
        api_url=target_url,
        model=request.model,
    )
    
    return TestConnectionResponse(
        success=success,
        message=message
    )


@router.post("/test-vision-connection", response_model=TestConnectionResponse)
async def test_vision_connection(
    request: TestVisionRequest,
    db: AsyncSession = Depends(settings_db),
    current_user: User = Depends(require_admin())
):
    """
    测试多模态视觉大模型连接
    
    需要管理员权限
    """
    resolved = await SettingsService.resolve_agri_model(db, "vision", request.api_key, request.base_url, request.model)

    result = await ModelProbeService.test(resolved, 'vision')
    return TestConnectionResponse(success=result['success'], message=result['message'])


@router.post("/test-weather-connection", response_model=TestConnectionResponse)
async def test_weather_connection(
    request: TestWeatherRequest,
    db: AsyncSession = Depends(settings_db),
    current_user: User = Depends(require_admin())
):
    """
    测试气象服务连接
    
    需要管理员权限
    """
    saved = await SettingsService.get_agri_ai_config(db)
    values = request.weather_custom.model_dump() if request.weather_custom is not None else saved.weather_custom
    same_scope = credential_identity(request.provider, values) == credential_identity(saved.weather_provider, saved.weather_custom)
    key = request.api_key if request.api_key is not None else saved.weather_api_key if same_scope else ''
    success, message = await SettingsService.test_weather_connection(provider=request.provider, api_key=key,
        custom_config=values, latitude=request.latitude, longitude=request.longitude)
    return TestConnectionResponse(success=success, message=message)

