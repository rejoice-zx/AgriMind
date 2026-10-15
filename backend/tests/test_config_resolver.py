# -*- coding: utf-8 -*-
"""DS-40 — 配置三源解析（DB / 环境变量 / 默认值）的最小统一。

审计结论是「三源及 fallback 都存在，未证明实际线上冲突；resolver 最小统一优先于
全局重写」。本文件把统一的判据固定下来，而不是重写配置体系：

- 解析优先级固定为 运行时注入 > 环境变量 > 默认值；
- 环境变量只作用于 _ENV_MAP 显式声明的键，不会误伤其它键；
- embedding / chunk / history 等**有效键**必须仍可解析（逐项保留，防止后续误删）；
- 数据库的 RAG 配置经 apply_rag_config 这**唯一入口**桥接进运行时。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Config, DB_BACKED_KEYS, _DEFAULTS
from app.database import Base
from app.services.settings_service import RAGConfig, SettingsService

# 当前代码库里被真实消费的有效键（见 core/config.py 的 DB_BACKED_KEYS 注释）。
CONSUMED_KEYS = (
    "api_key",
    "api_base_url",
    "embedding_model",
    "chunk_size",
    "chunk_overlap",
    "chunk_max_per_item",
    "history_max_messages",
    "history_max_chars",
    "context_top_n",
    "retrieval_top_k",
    "similarity_threshold",
    "context_max_chars",
)


@pytest.fixture
def runtime_config():
    """单例 Config，用完把运行时注入还原，避免污染同会话的其它测试。"""
    cfg = Config()
    saved = dict(cfg._runtime)
    cfg.reload()
    try:
        yield cfg
    finally:
        cfg._runtime.clear()
        cfg._runtime.update(saved)


@pytest.mark.parametrize("key", CONSUMED_KEYS)
def test_every_consumed_key_still_resolves(runtime_config, key):
    """有效键一个都不能丢：删掉任何一项都会让对应功能静默回落或崩掉。"""
    assert runtime_config.get(key) is not None


@pytest.mark.parametrize("key", CONSUMED_KEYS)
def test_defaults_are_the_lowest_priority_source(runtime_config, key):
    assert runtime_config.get(key) == _DEFAULTS[key]


def test_env_override_wins_over_defaults(runtime_config, monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "env-only-key")
    assert runtime_config.get("api_key") == "env-only-key"


def test_runtime_injection_wins_over_env_and_defaults(runtime_config, monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "env-only-key")
    runtime_config.set("api_key", "runtime-key")
    assert runtime_config.get("api_key") == "runtime-key"


def test_env_lookup_only_applies_to_mapped_keys(runtime_config, monkeypatch):
    """没有写进 _ENV_MAP 的键不受同名环境变量影响（否则部署环境会意外改行为）。"""
    monkeypatch.setenv("CHUNK_SIZE", "123")
    assert runtime_config.get("chunk_size") == _DEFAULTS["chunk_size"]


def test_apply_rag_config_bridges_the_database_values(runtime_config):
    runtime_config.apply_rag_config(RAGConfig(chunk_size=111, chunk_overlap=22))
    assert runtime_config.get("chunk_size") == 111
    assert runtime_config.get("chunk_overlap") == 22


def test_apply_rag_config_leaves_other_keys_untouched(runtime_config):
    before = {key: runtime_config.get(key) for key in CONSUMED_KEYS}
    runtime_config.apply_rag_config(RAGConfig(chunk_size=111, chunk_overlap=22))
    for key in CONSUMED_KEYS:
        if key in DB_BACKED_KEYS:
            continue
        assert runtime_config.get(key) == before[key]


def test_db_backed_keys_are_declared_defaults(runtime_config):
    assert DB_BACKED_KEYS
    assert set(DB_BACKED_KEYS) <= set(_DEFAULTS)


@pytest.mark.asyncio
async def test_database_rag_config_reaches_the_runtime_config(runtime_config):
    """真实 DB 源：写进设置表再读回来，经唯一入口桥接后运行时可见。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            await SettingsService.set_rag_config(db, RAGConfig(chunk_size=321, chunk_overlap=45))
            stored = await SettingsService.get_rag_config(db)
            assert (stored.chunk_size, stored.chunk_overlap) == (321, 45)

            runtime_config.apply_rag_config(stored)
        assert runtime_config.get("chunk_size") == 321
        assert runtime_config.get("chunk_overlap") == 45
        # 未被 DB 覆盖的键仍是默认值，不会被这次桥接顺手改掉
        assert runtime_config.get("history_max_messages") == _DEFAULTS["history_max_messages"]
    finally:
        await engine.dispose()
