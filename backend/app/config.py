"""Application Configuration"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
import os
from dotenv import load_dotenv

DEFAULT_JWT_SECRET = "your-secret-key-change-in-production"

# 基于代码物理位置计算项目根目录与绝对数据目录，彻底杜绝 CWD 路径漂移导致 data 分裂
APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
BASE_DIR = BACKEND_DIR.parent
DEFAULT_DATA_DIR = str(BASE_DIR / "data")
DEFAULT_LOGS_DIR = str(BASE_DIR / "logs")
DEFAULT_DB_PATH = (BASE_DIR / "data" / "app.db").as_posix()
DEFAULT_DATABASE_URL = f"sqlite+aiosqlite:///{DEFAULT_DB_PATH}"

# 显式加载 .env 文件（override=False 避免覆盖外部系统注入的环境变量）
for _env_path in (BASE_DIR / ".env", BACKEND_DIR / ".env"):
    if _env_path.exists():
        load_dotenv(_env_path, override=False)


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    model_config = SettingsConfigDict(
        env_file=[str(BASE_DIR / ".env"), str(BACKEND_DIR / ".env"), ".env"],
        case_sensitive=True,
        extra="ignore"
    )
    
    # Application
    APP_NAME: str = "AgriMind农业智能决策与服务平台"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False
    
    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    
    # CORS
    CORS_ORIGINS: List[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080"
    ]
    
    # Database
    DATABASE_URL: str = DEFAULT_DATABASE_URL
    
    # JWT Authentication
    JWT_SECRET_KEY: str = DEFAULT_JWT_SECRET
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 120  # 2 hours
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # 新配置密文必须使用独立密钥；JWT 派生密钥仅用于历史密文读取。
    SETTINGS_ENCRYPTION_KEY: str = ""
    
    # Rate Limiting
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    
    # Paths (全站绝对路径规范)
    BASE_DIR: str = str(BASE_DIR)
    DATA_DIR: str = DEFAULT_DATA_DIR
    LOGS_DIR: str = DEFAULT_LOGS_DIR

    # AgriMind AI 智能决策、视觉与外部气象配置
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.siliconflow.cn/v1"
    LLM_MODEL: str = "deepseek-ai/DeepSeek-V3"
    VISION_API_KEY: str = ""
    VISION_BASE_URL: str = "https://api.siliconflow.cn/v1"
    VISION_MODEL: str = "Qwen/Qwen3-VL-8B-Instruct"
    WEATHER_API_KEY: str = ""
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = "https://api.siliconflow.cn/v1"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIMENSION: int = 1024


settings = Settings()


INSECURE_JWT_SECRETS = {
    DEFAULT_JWT_SECRET,
    "agrimind-dev-jwt-secret-key-2026",
    "secret",
    "secret123",
    "123456",
    "password",
    "admin123",
}


def is_insecure_jwt_secret(secret: str) -> bool:
    normalized = (secret or "").strip()
    if not normalized or len(normalized) < 16:
        return True
    lower = normalized.lower()
    if lower in {s.lower() for s in INSECURE_JWT_SECRETS}:
        return True
    if any(p in lower for p in ["change-in-production", "agrimind-dev", "default-secret"]):
        return True
    return False


def is_production_environment() -> bool:
    app_env = (os.getenv("APP_ENV") or os.getenv("ENV") or "").strip().lower()
    # Only explicit local/test modes opt out of production protections. Missing,
    # misspelled and staging modes must not enable demo accounts or public docs.
    return app_env not in {"development", "dev", "test", "testing", "local"}


def validate_startup_configuration() -> None:
    """Reject unsafe bootstrap configuration before creating directories/tables."""
    if is_production_environment():
        if is_insecure_jwt_secret(settings.JWT_SECRET_KEY):
            raise RuntimeError("JWT_SECRET_KEY is insecure in production environment")
        if len(settings.SETTINGS_ENCRYPTION_KEY.strip()) < 32:
            raise RuntimeError("Production requires an independent SETTINGS_ENCRYPTION_KEY of at least 32 characters")
    username = os.getenv("INITIAL_ADMIN_USERNAME", "").strip()
    password = os.getenv("INITIAL_ADMIN_PASSWORD", "").strip()
    if bool(username) != bool(password):
        raise RuntimeError("INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD must be configured together")
    if username and (len(username) > 50 or len(password) < 12 or len(password.encode("utf-8")) > 72):
        raise RuntimeError("Invalid initial administrator configuration: username <=50 characters, password 12 characters to 72 UTF-8 bytes")
