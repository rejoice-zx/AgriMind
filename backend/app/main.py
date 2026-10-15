"""FastAPI Application Entry Point"""
import os
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
from app.core.audit_logging import audit
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.config import settings, is_insecure_jwt_secret, is_production_environment, validate_startup_configuration
from app.database import init_db, get_db
from sqlalchemy.exc import IntegrityError


from app.core.audit_logging import AuditFormatter as _TzAwareFormatter


# 全局 formatter 实例，供 lifespan 和 settings API 更新时区
_log_formatter = _TzAwareFormatter(
    fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    tz_name="Asia/Shanghai",
)


def get_log_formatter() -> _TzAwareFormatter:
    """获取全局日志格式化器（供外部模块更新时区）"""
    return _log_formatter


def setup_logging():
    """配置日志写入文件"""
    log_dir = settings.LOGS_DIR
    if not os.path.isabs(log_dir):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        log_dir = os.path.join(base_dir, log_dir)
    os.makedirs(log_dir, exist_ok=True)

    root_logger = logging.getLogger()
    if any(getattr(handler, "agri_owned", False) for handler in root_logger.handlers):
        return

    # 应用日志 - app.log (INFO+)
    app_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(_log_formatter)

    # 错误日志 - error.log (ERROR+)
    error_handler = RotatingFileHandler(
        os.path.join(log_dir, "error.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(_log_formatter)

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(_log_formatter)

    # 配置根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)
    for handler in (app_handler, error_handler, console_handler):
        handler.agri_owned = True
    root_logger.addHandler(app_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(console_handler)


setup_logging()
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.agri_performance import AgriPerformanceMiddleware
from app.middleware.audit_context import AuditContextMiddleware
from app.api import (
    auth_router,
    knowledge_router,
    statistics_router,
    performance_router,
    logs_router,
    settings_router,
    backup_router,
    farms_router,
    farming_records_router,
    agri_agent_router,
    agri_history_router,
    expert_workbench_router,
    agri_plan_router,
)
from app.api.users import router as users_router
from app.api.expert_workbench import legacy_media_router




@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    validate_startup_configuration()
    # Startup: Initialize database
    await init_db()

    # 从数据库同步时区设置到日志格式化器
    try:
        from app.database import get_db as _get_db_gen
        async for db in _get_db_gen():
            from app.services.settings_service import SettingsService
            general = await SettingsService.get_general_config(db)
            if general.timezone:
                _log_formatter.set_timezone(general.timezone)
            break
    except Exception:
        pass  # 首次启动数据库可能为空，使用默认时区
    
    # Security check for JWT secret key
    _logger = logging.getLogger(__name__)
    if is_insecure_jwt_secret(settings.JWT_SECRET_KEY):
        if is_production_environment():
            raise RuntimeError(
                "JWT_SECRET_KEY is insecure in production environment. "
                "Set a strong JWT_SECRET_KEY via environment variables before startup."
            )
        _logger.warning(
            "JWT_SECRET_KEY is still using the default insecure value. "
            "Set a strong key in .env before deploying to production."
        )
    
    yield
    # Shutdown: Cleanup if needed


def interactive_docs_urls(production: bool) -> dict:
    """Interactive API docs are a development affordance only (P2-DEPLOY / DS-07).

    In production the schema/`/docs`/`/redoc` routes are switched off so the API
    surface is not published to anonymous callers.
    """
    if production:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AgriMind 农业智能决策与农技知识 RAG 服务（Vue3 + FastAPI）",
    **interactive_docs_urls(is_production_environment()),
    lifespan=lifespan
)

@app.exception_handler(IntegrityError)
async def data_integrity_error(request: Request, error: IntegrityError):
    message = str(error.orig)
    if 'Agricultural archive is read only' in message:
        detail = '该农场或田块已归档，只能查看历史，请恢复后再修改'
    elif 'Agricultural history is protected' in message:
        detail = '为保护农业追溯历史，物理删除已停用，请使用归档'
    elif 'Field observations are append only' in message:
        detail = '现场观测保留原始历史，请追加新观测或更正说明'
    elif 'Crop status history is append only' in message:
        detail = '批次状态历史只能追加，不能覆盖或删除'
    elif any(value in message for value in ('Signed consultation reviews are immutable',
            'Consultation review history is protected', 'Resolved consultations are immutable')):
        detail = '已签署答复及复核回执保留原始历史，不能覆盖或删除；需另发咨询重新复核'
    else:
        detail = '记录关系或唯一性约束冲突，请刷新后核对'
    return JSONResponse(status_code=409, content={'detail': detail})


@app.exception_handler(Exception)
async def internal_error_response(request: Request, error: Exception):
    request_id = getattr(request.state, "request_id", None)
    audit("http_server_error", request_id=request_id, status_code=500, error_type=type(error).__name__)
    return JSONResponse(status_code=500,
        content={"detail": "服务器处理失败，请凭请求ID核查", "request_id": request_id},
        headers={"X-Request-ID": request_id} if request_id else {})


# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

# Rate limiting
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AgriPerformanceMiddleware)
app.add_middleware(AuditContextMiddleware)

# Include API routers
app.include_router(auth_router)
app.include_router(knowledge_router)
app.include_router(statistics_router)
app.include_router(performance_router)
app.include_router(logs_router)
app.include_router(settings_router)
app.include_router(backup_router)
app.include_router(users_router)
app.include_router(farms_router)
app.include_router(farming_records_router)
app.include_router(agri_agent_router)
app.include_router(agri_history_router)
app.include_router(expert_workbench_router)
app.include_router(agri_plan_router)

# Mount static uploads directory for crop disease images (使用绝对路径)
uploads_dir = os.path.join(settings.DATA_DIR, "uploads")
diagnosis_dir = os.path.join(uploads_dir, "diagnosis")
os.makedirs(diagnosis_dir, exist_ok=True)
app.include_router(legacy_media_router)




@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running"
    }


@app.get("/health")
async def health_check():
    """Process liveness only; use /ready for local data readiness."""
    return {"status": "healthy"}


@app.get("/ready")
async def readiness_check(db=Depends(get_db)):
    """Local schema, candidate and root-directory checks; no provider/model calls."""
    from sqlalchemy import select
    from app.models.knowledge import KnowledgeItem
    from app.database import Base
    from app.core.schema_revision import verify_runtime_schema, CURRENT_SCHEMA_REVISION
    from app.core.storage_readiness import storage_readiness
    import asyncio
    from app.core.knowledge_contract import candidate_eligible
    from app.core.weather_policy import agriculture_today
    from app.services.agri_retrieval_service import AgriRetrievalService
    try:
        connection = await db.connection()
        if connection.dialect.name != 'sqlite':
            raise ValueError('Local readiness supports the deployed SQLite contract only')
        await connection.run_sync(lambda sync: verify_runtime_schema(sync, Base.metadata))
        rows = (await db.scalars(select(KnowledgeItem).where(
            KnowledgeItem.is_current == 1, KnowledgeItem.crop.is_not(None), KnowledgeItem.crop != ''))).all()
        eligible = sum(candidate_eligible(row, agriculture_today()) for row in rows)
        await AgriRetrievalService.config(db)
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unavailable_or_incompatible"})
    log_dir = settings.LOGS_DIR
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), log_dir)
    storage = await asyncio.to_thread(storage_readiness, {'data': settings.DATA_DIR, 'logs': log_dir})
    ready = bool(eligible) and all(state == 'writable' for state in storage.values())
    return JSONResponse(status_code=200 if ready else 503, content={
        "status": "ready" if ready else "degraded", "database": "ready", "schema_revision": CURRENT_SCHEMA_REVISION,
        "storage": storage, "storage_probe_cache_seconds": 30,
        "knowledge_candidates": eligible, "source_verification": "manual_required",
        "external_providers": "not_probed", "field_validation": "not_assessed",
    })
