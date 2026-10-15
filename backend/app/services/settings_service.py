# -*- coding: utf-8 -*-
"""
设置管理服务模块

功能:
- 系统设置的读取和更新
- LLM提供商配置
- API密钥加密存储
- API连接测试
"""

import os
import asyncio
import json
import logging
import base64
import hashlib
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.models.settings import SystemSettings
from app.core.llm_providers import get_all_providers, get_provider
from app.config import settings as app_settings
from app.utils.time import utcnow
from app.utils.security import validate_safe_external_url

logger = logging.getLogger(__name__)


class SettingsEncryptionError(RuntimeError):
    """配置加密失败——拒绝把明文落库。"""


class SettingsCipherUnavailable(RuntimeError):
    """配置密文无法解密（密钥不匹配或数据损坏）——调用方须按“不可用”处理。"""


_CIPHER_V2_PREFIX = "v2:"
_CIPHER_V3_PREFIX = "v3:"


def _get_encryption_key(legacy: bool = False) -> bytes:
    """返回配置加密密钥的 Fernet 形式。

    新写入必须使用独立 SETTINGS_ENCRYPTION_KEY。JWT 派生密钥仅用于历史读取。
    """
    primary = (getattr(app_settings, "SETTINGS_ENCRYPTION_KEY", "") or "").strip()
    if not legacy:
        if not primary:
            raise SettingsEncryptionError("未配置独立 SETTINGS_ENCRYPTION_KEY，拒绝写入配置密文")
        return base64.urlsafe_b64encode(hashlib.sha256(primary.encode()).digest())
    salt = b'settings_encryption_salt'
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(app_settings.JWT_SECRET_KEY.encode()))


def _encrypt_value(value: str) -> str:
    """加密敏感值。

    加密失败时**抛 SettingsEncryptionError**，绝不返回明文——调用方须据此中止写入
    （fail-closed）。v3 明确表示独立密钥，不再写入存在密钥来源歧义的 v2。
    """
    if not value:
        return value
    try:
        if len((app_settings.SETTINGS_ENCRYPTION_KEY or '').strip()) < 32:
            raise SettingsEncryptionError('独立 SETTINGS_ENCRYPTION_KEY 至少需要32字符')
        token = Fernet(_get_encryption_key()).encrypt(value.encode()).decode()
        return _CIPHER_V3_PREFIX + token
    except Exception as e:
        logger.error("配置加密失败，已拒绝写入以避免明文落库: %s", e)
        raise SettingsEncryptionError("配置加密失败，已中止写入") from e


def _decrypt_value(encrypted: str) -> str:
    """解密敏感值。

    v3 只允许独立密钥；历史 v2 曾错误混用独立/JWT 密钥，因此只对 v2 双读。
    无前缀历史密文使用 JWT 派生密钥。重新保存时升级为独立密钥 v3。
    解密失败时**抛 SettingsCipherUnavailable**（不再把密文当明文返回）。
    """
    if not encrypted:
        return encrypted
    if encrypted.startswith(_CIPHER_V3_PREFIX):
        try:
            return Fernet(_get_encryption_key()).decrypt(
                encrypted[len(_CIPHER_V3_PREFIX):].encode()
            ).decode()
        except Exception as e:
            logger.error("配置解密失败(v3)，密钥不匹配或数据损坏")
            raise SettingsCipherUnavailable("配置密文无法解密（密钥不匹配或数据损坏）") from e
    if encrypted.startswith(_CIPHER_V2_PREFIX):
        payload = encrypted[len(_CIPHER_V2_PREFIX):]
        for legacy in (False, True):
            try:
                return Fernet(_get_encryption_key(legacy=legacy)).decrypt(payload.encode()).decode()
            except Exception:
                continue
        raise SettingsCipherUnavailable("历史 v2 配置密文无法解密，需要原密钥恢复或重新配置")
    try:
        return Fernet(_get_encryption_key(legacy=True)).decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.error("配置解密失败(legacy v1): %s", e)
        raise SettingsCipherUnavailable("配置密文无法解密（密钥不匹配或数据损坏）") from e


@dataclass
class LLMConfig:
    """LLM配置"""
    provider: str = "siliconflow"
    api_key: str = ""
    api_url: str = ""
    model: str = ""


@dataclass
class EmbeddingConfig:
    """Embedding配置"""
    provider: str = "siliconflow"  # 使用与LLM相同的提供商
    api_key: str = ""  # 空值只允许按同一API根地址解析兼容密钥来源
    api_url: str = ""
    model: str = "BAAI/bge-large-zh-v1.5"
    dimension: int = 1024
    request_dimensions: bool = False


@dataclass
class RAGConfig:
    """RAG配置"""
    top_k: int = 5
    similarity_threshold: float = 0.5
    use_rewrite: bool = True
    max_context_length: int = 2000
    chunk_size: int = 500
    chunk_overlap: int = 50


@dataclass
class GeneralConfig:
    """通用配置"""
    timezone: str = "Asia/Shanghai"


@dataclass
class AgriAIConfig:
    """农业AI大模型、多模态视觉与真实气象服务配置"""
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "deepseek-ai/DeepSeek-V3"))
    vision_api_key: str = field(default_factory=lambda: os.getenv("VISION_API_KEY", ""))
    vision_base_url: str = field(default_factory=lambda: os.getenv("VISION_BASE_URL", "https://api.siliconflow.cn/v1"))
    vision_model: str = field(default_factory=lambda: os.getenv("VISION_MODEL", "Qwen/Qwen3-VL-8B-Instruct"))
    weather_provider: str = "open_meteo"
    weather_custom: dict = field(default_factory=dict)
    weather_api_key: str = field(default_factory=lambda: os.getenv("WEATHER_API_KEY", ""))


@dataclass
class SystemConfig:
    """系统配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    general: GeneralConfig = field(default_factory=GeneralConfig)
    agri_ai: AgriAIConfig = field(default_factory=AgriAIConfig)


class SettingsService:
    """设置管理服务"""
    
    # 设置键名
    KEY_LLM_CONFIG = "llm_config"
    KEY_EMBEDDING_CONFIG = "embedding_config"
    KEY_RAG_CONFIG = "rag_config"
    KEY_GENERAL_CONFIG = "general_config"
    KEY_AGRI_AI_CONFIG = "agri_ai_config"
    
    # 敏感字段（需要加密）
    SENSITIVE_FIELDS = ["api_key"]
    
    @classmethod
    async def get_setting(
        cls, 
        db: AsyncSession, 
        key: str
    ) -> Optional[str]:
        """获取单个设置值
        
        Args:
            db: 数据库会话
            key: 设置键名
            
        Returns:
            设置值或None
        """
        result = await db.execute(
            select(SystemSettings).where(SystemSettings.key == key).execution_options(populate_existing=True)
        )
        setting = result.scalar_one_or_none()
        return setting.value if setting else None
    
    @classmethod
    async def set_setting(
        cls, 
        db: AsyncSession, 
        key: str, 
        value: str,
        *, commit: bool = True
    ):
        """设置单个值
        
        Args:
            db: 数据库会话
            key: 设置键名
            value: 设置值
        """
        result = await db.execute(
            select(SystemSettings).where(SystemSettings.key == key).execution_options(populate_existing=True)
        )
        setting = result.scalar_one_or_none()
        
        if setting:
            setting.value = value
            setting.updated_at = utcnow()
        else:
            setting = SystemSettings(key=key, value=value)
            db.add(setting)
        
        if commit:
            await db.commit()
            if key in {cls.KEY_EMBEDDING_CONFIG, cls.KEY_LLM_CONFIG, cls.KEY_RAG_CONFIG, cls.KEY_AGRI_AI_CONFIG}:
                from app.core.config import Config
                Config.invalidate_embedding()
        else:
            await db.flush()
    
    @classmethod
    async def get_llm_config(cls, db: AsyncSession) -> LLMConfig:
        """获取LLM配置
        
        Args:
            db: 数据库会话
            
        Returns:
            LLMConfig: LLM配置
        """
        value = await cls.get_setting(db, cls.KEY_LLM_CONFIG)
        
        if not value:
            return LLMConfig()
        
        try:
            data = json.loads(value)
            # 解密API密钥
            if "api_key" in data and data["api_key"]:
                data["api_key"] = _decrypt_value(data["api_key"])
            return LLMConfig(**data)
        except SettingsCipherUnavailable:
            raise
        except Exception as e:
            logger.error("解析LLM配置失败: %s", str(e))
            return LLMConfig()
    
    @classmethod
    async def set_llm_config(
        cls, 
        db: AsyncSession, 
        config: LLMConfig
    ):
        """设置LLM配置
        
        Args:
            db: 数据库会话
            config: LLM配置
        """
        data = asdict(config)
        # 加密API密钥
        if data.get("api_key"):
            data["api_key"] = _encrypt_value(data["api_key"])
        
        await cls.set_setting(db, cls.KEY_LLM_CONFIG, json.dumps(data))

    @classmethod
    async def get_embedding_config(cls, db: AsyncSession) -> EmbeddingConfig:
        """获取Embedding配置
        
        Args:
            db: 数据库会话
            
        Returns:
            EmbeddingConfig: Embedding配置
        """
        value = await cls.get_setting(db, cls.KEY_EMBEDDING_CONFIG)
        
        if not value:
            return EmbeddingConfig()
        
        try:
            data = json.loads(value)
            # 解密API密钥
            if "api_key" in data and data["api_key"]:
                data["api_key"] = _decrypt_value(data["api_key"])
            return EmbeddingConfig(**data)
        except SettingsCipherUnavailable:
            raise
        except Exception as e:
            logger.error("解析Embedding配置失败: %s", type(e).__name__)
            return EmbeddingConfig()

    @classmethod
    async def get_embedding_config_with_env(cls, db: AsyncSession) -> EmbeddingConfig:
        """获取Embedding配置，若数据库未单独配置则从环境变量与设置回落
        
        Args:
            db: 数据库会话
            
        Returns:
            EmbeddingConfig: 填充后的Embedding配置
        """
        raw = await cls.get_setting(db, cls.KEY_EMBEDDING_CONFIG)
        cfg = await cls.get_embedding_config(db)
        # A persisted model remains authoritative even when it happens to equal an old default.
        if raw is not None:
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict) or not set(parsed) <= {"provider", "api_key", "api_url", "model", "dimension", "request_dimensions"}: raise ValueError()
                for name in ("provider", "api_url", "model", "api_key"):
                    if name in parsed and not isinstance(parsed[name], str): raise ValueError()
            except (ValueError, TypeError):
                raise SettingsCipherUnavailable("Saved embedding configuration is invalid") from None
        provider_url = get_provider(cfg.provider).default_api_url
        env_url = getattr(app_settings, "EMBEDDING_BASE_URL", "") or provider_url
        url = (cfg.api_url or (provider_url if raw is not None else env_url)).strip().rstrip("/")
        key = cfg.api_key
        source = "saved_embedding" if key else "missing"
        if not key:
            agri = await cls.get_agri_ai_config(db)
            if url == agri.llm_base_url.strip().rstrip("/"):
                key = agri.llm_api_key
                if key: source = "agricultural_decision"
        if not key:
            # Read-only compatibility with old installations; never forward across addresses.
            llm = await cls.get_llm_config(db)
            llm_url = (llm.api_url or get_provider(llm.provider).default_api_url).strip().rstrip("/")
            if url == llm_url:
                key = llm.api_key
                if key: source = "legacy_shared"
        if not key and url == env_url.strip().rstrip("/"):
            key = getattr(app_settings, "EMBEDDING_API_KEY", "")
            if key: source = "environment_embedding"
        env_llm_url = getattr(app_settings, "LLM_BASE_URL", "").strip().rstrip("/")
        if not key and url == env_llm_url:
            key = getattr(app_settings, "LLM_API_KEY", "")
            if key: source = "environment_llm"
        model = cfg.model if raw is not None else getattr(app_settings, "EMBEDDING_MODEL", "") or cfg.model
        dimension = cfg.dimension if raw is not None else getattr(app_settings, "EMBEDDING_DIMENSION", 1024)
        if (not isinstance(model, str) or not model.strip() or type(dimension) is not int
                or not 1 <= dimension <= 4096 or type(cfg.request_dimensions) is not bool):
            raise SettingsCipherUnavailable("Saved embedding model or dimension is invalid")
        effective = EmbeddingConfig(provider=cfg.provider, api_key=key or "", api_url=url, model=model.strip(),
            dimension=dimension, request_dimensions=cfg.request_dimensions)
        effective.credential_source = source
        effective.config_origin = "saved" if raw is not None else "environment_defaults"
        return effective

    @classmethod
    async def set_embedding_config(
        cls, 
        db: AsyncSession, 
        config: EmbeddingConfig
    ):
        """设置Embedding配置
        
        Args:
            db: 数据库会话
            config: Embedding配置
        """
        data = asdict(config)
        # 保存侧 SSRF 校验：非空 api_url 必须是安全的外部地址（与 LLM/视觉同一套防护）
        if data.get("api_url"):
            is_safe, err_msg = validate_safe_external_url(data["api_url"])
            if not is_safe:
                raise ValueError(f"Embedding API 地址不安全: {err_msg}")
        # 加密API密钥
        if data.get("api_key"):
            data["api_key"] = _encrypt_value(data["api_key"])

        await cls.set_setting(db, cls.KEY_EMBEDDING_CONFIG, json.dumps(data))

    @classmethod
    async def get_rag_config(cls, db: AsyncSession) -> RAGConfig:
        """获取RAG配置
        
        Args:
            db: 数据库会话
            
        Returns:
            RAGConfig: RAG配置
        """
        value = await cls.get_setting(db, cls.KEY_RAG_CONFIG)
        
        if not value:
            return RAGConfig()
        
        try:
            data = json.loads(value)
            return RAGConfig(**data)
        except Exception as e:
            logger.error("解析RAG配置失败: %s", str(e))
            return RAGConfig()
    
    @classmethod
    async def set_rag_config(
        cls, 
        db: AsyncSession, 
        config: RAGConfig
    ):
        """设置RAG配置
        
        Args:
            db: 数据库会话
            config: RAG配置
        """
        await cls.set_setting(
            db, 
            cls.KEY_RAG_CONFIG, 
            json.dumps(asdict(config))
        )
    
    @classmethod
    async def get_agri_ai_config(cls, db: AsyncSession) -> AgriAIConfig:
        """获取农业AI与外部服务配置"""
        value = await cls.get_setting(db, cls.KEY_AGRI_AI_CONFIG)
        if not value:
            return AgriAIConfig()

        try:
            data = json.loads(value)
            if "llm_api_key" in data and data["llm_api_key"]:
                data["llm_api_key"] = _decrypt_value(data["llm_api_key"])
            if "vision_api_key" in data and data["vision_api_key"]:
                data["vision_api_key"] = _decrypt_value(data["vision_api_key"])
            if "weather_api_key" in data and data["weather_api_key"]:
                data["weather_api_key"] = _decrypt_value(data["weather_api_key"])
            return AgriAIConfig(**data)
        except SettingsCipherUnavailable:
            raise
        except Exception as e:
            logger.error("解析农业AI配置失败: %s", str(e))
            return AgriAIConfig()

    @staticmethod
    def endpoint_key(saved_url, saved_key, new_url, explicit_key):
        if explicit_key is not None: return explicit_key
        return saved_key if new_url.strip().rstrip("/") == saved_url.strip().rstrip("/") else ""

    @classmethod
    async def resolve_agri_model(cls, db: AsyncSession, kind: str, api_key=None, base_url=None, model=None):
        config = await cls.get_agri_ai_config(db)
        if kind == "vision":
            saved_url, saved_key, saved_model = config.vision_base_url.strip().rstrip("/"), config.vision_api_key, config.vision_model
            if not saved_key and saved_url == config.llm_base_url.strip().rstrip("/"):
                saved_key = config.llm_api_key
        elif kind == "decision":
            saved_url, saved_key, saved_model = config.llm_base_url.strip().rstrip("/"), config.llm_api_key, config.llm_model
        else:
            raise ValueError("未知的农业模型用途")
        url = (base_url if base_url is not None else saved_url).strip().rstrip("/")
        key = cls.endpoint_key(saved_url, saved_key, url, api_key)
        resolved_model = model if model is not None else saved_model if url == saved_url else ""
        return {"api_key": key, "base_url": url, "model_name": resolved_model}

    @classmethod
    async def set_agri_ai_config(cls, db: AsyncSession, config: AgriAIConfig):
        """设置农业AI与外部服务配置"""
        data = asdict(config)
        if data.get("llm_api_key"):
            data["llm_api_key"] = _encrypt_value(data["llm_api_key"])
        if data.get("vision_api_key"):
            data["vision_api_key"] = _encrypt_value(data["vision_api_key"])
        if data.get("weather_api_key"):
            data["weather_api_key"] = _encrypt_value(data["weather_api_key"])
        await cls.set_setting(db, cls.KEY_AGRI_AI_CONFIG, json.dumps(data))

    @classmethod
    async def get_all_settings(cls, db: AsyncSession) -> SystemConfig:
        """获取所有设置
        
        Args:
            db: 数据库会话
            
        Returns:
            SystemConfig: 系统配置
        """
        llm_config = await cls.get_llm_config(db)
        embedding_config = await cls.get_embedding_config(db)
        rag_config = await cls.get_rag_config(db)
        general_config = await cls.get_general_config(db)
        agri_ai_config = await cls.get_agri_ai_config(db)
        
        return SystemConfig(
            llm=llm_config, 
            embedding=embedding_config, 
            rag=rag_config, 
            general=general_config,
            agri_ai=agri_ai_config
        )
    
    @classmethod
    async def get_general_config(cls, db: AsyncSession) -> GeneralConfig:
        """获取通用配置"""
        value = await cls.get_setting(db, cls.KEY_GENERAL_CONFIG)
        if not value:
            return GeneralConfig()
        try:
            data = json.loads(value)
            return GeneralConfig(**data)
        except Exception as e:
            logger.error("解析通用配置失败: %s", str(e))
            return GeneralConfig()

    @classmethod
    async def set_general_config(cls, db: AsyncSession, config: GeneralConfig):
        """设置通用配置"""
        await cls.set_setting(db, cls.KEY_GENERAL_CONFIG, json.dumps(asdict(config)))

    @classmethod
    async def update_settings(
        cls, 
        db: AsyncSession, 
        llm_config: Optional[LLMConfig] = None,
        embedding_config: Optional[EmbeddingConfig] = None,
        rag_config: Optional[RAGConfig] = None,
        general_config: Optional[GeneralConfig] = None,
        agri_ai_config: Optional[AgriAIConfig] = None,
    ):
        """更新设置
        
        Args:
            db: 数据库会话
            llm_config: LLM配置（可选）
            embedding_config: Embedding配置（可选）
            rag_config: RAG配置（可选）
            general_config: 通用配置（可选）
            agri_ai_config: 农业AI配置（可选）
        """
        # Validate and encrypt the whole request before staging any DB changes.
        # Per-section commits used to leave a partial update when a later key or
        # URL failed. The one final commit publishes all sections together.
        prepared = []
        for key, config in (
            (cls.KEY_LLM_CONFIG, llm_config),
            (cls.KEY_EMBEDDING_CONFIG, embedding_config),
            (cls.KEY_RAG_CONFIG, rag_config),
            (cls.KEY_GENERAL_CONFIG, general_config),
            (cls.KEY_AGRI_AI_CONFIG, agri_ai_config),
        ):
            if config is None:
                continue
            data = asdict(config)
            for name in ("api_url", "llm_base_url", "vision_base_url"):
                if data.get(name):
                    is_safe, err_msg = validate_safe_external_url(data[name])
                    if not is_safe:
                        raise ValueError(f"API 地址安全校验未通过: {err_msg}")
            for name in ("api_key", "llm_api_key", "vision_api_key", "weather_api_key"):
                if data.get(name):
                    data[name] = _encrypt_value(data[name])
            prepared.append((key, json.dumps(data)))
        if not prepared:
            return
        try:
            for key, value in prepared:
                await cls.set_setting(db, key, value, commit=False)
            await db.commit()
            if any(key in {cls.KEY_EMBEDDING_CONFIG, cls.KEY_LLM_CONFIG, cls.KEY_RAG_CONFIG, cls.KEY_AGRI_AI_CONFIG} for key, _ in prepared):
                from app.core.config import Config
                Config.invalidate_embedding()
        except Exception:
            await db.rollback()
            raise
    
    @classmethod
    def get_llm_providers(cls) -> List[Dict[str, Any]]:
        """获取所有LLM提供商信息
        
        Returns:
            List[Dict]: 提供商列表
        """
        return get_all_providers()
    
    @classmethod
    async def test_llm_connection(
        cls,
        provider: str,
        api_key: str,
        api_url: Optional[str] = None,
        model: Optional[str] = None
    ) -> tuple:
        """测试LLM API连接
        
        Args:
            provider: 提供商名称
            api_key: API密钥
            api_url: API地址（可选）
            model: 模型名称（可选）
            
        Returns:
            tuple: (成功标志, 消息)
        """
        from app.services.model_probe_service import ModelProbeService
        try:
            provider_class = get_provider(provider)
        except ValueError:
            return False, "未知模型提供商"
        result = await ModelProbeService.test({"api_key": api_key,
            "base_url": api_url or provider_class.default_api_url,
            "model_name": model or provider_class.default_model}, "decision")
        return result["success"], result["message"]

    @classmethod
    def mask_api_key(cls, api_key: str) -> str:
        """遮蔽API密钥（用于显示）
        
        Args:
            api_key: API密钥
            
        Returns:
            str: 遮蔽后的密钥
        """
        if not api_key or len(api_key) < 8:
            return "****"
        return api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:]

    @classmethod
    async def test_vision_connection(
        cls,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ) -> tuple:
        """测试多模态视觉模型 API 连接"""
        if base_url:
            is_safe, err_msg = validate_safe_external_url(base_url)
            if not is_safe:
                return False, f"安全拦截：{err_msg}"
        from app.services.vision_service import VisionService
        return await VisionService.test_connection(
            api_key=api_key,
            base_url=base_url,
            model_name=model
        )

    @classmethod
    async def test_weather_connection(
        cls,
        provider: str = "open_meteo",
        api_key: Optional[str] = None, custom_config=None, latitude=None, longitude=None
    ) -> tuple:
        """测试气象服务 API 连接"""
        from app.services.weather_service import WeatherService
        return await WeatherService.test_connection(
            provider=provider,
            api_key=api_key, custom_config=custom_config, latitude=latitude, longitude=longitude
        )

