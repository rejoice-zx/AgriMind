# -*- coding: utf-8 -*-
"""
向量存储服务模块 - 封装FAISS向量存储和Embedding服务

功能:
- 复用现有core/vector_store.py和core/embedding.py
- 提供异步调用接口
- 支持向量索引管理
"""

import asyncio
import logging
import hashlib
import json
from collections import OrderedDict
from dataclasses import asdict
from app.core.embedding_contract import make_contract, fingerprint
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)

# 延迟导入core模块
_vector_store = None
_embedding_client = None
_executor = ThreadPoolExecutor(max_workers=4)


def _get_vector_store():
    """获取向量存储实例（延迟加载）"""
    global _vector_store
    if _vector_store is None:
        from app.core.vector_store import VectorStore
        _vector_store = VectorStore()
    return _vector_store


def _get_embedding_client():
    """获取Embedding客户端实例（延迟加载）"""
    global _embedding_client
    if _embedding_client is None:
        from app.core.embedding import EmbeddingClient
        _embedding_client = EmbeddingClient()
    return _embedding_client


class VectorService:
    """向量服务类 - 异步封装"""
    _query_embed_cache: Dict[str, List[float]] = {}  # Legacy cache is only revoked, never reused by bound requests.
    
    def __init__(self):
        """初始化向量服务"""
        self._loop = None
        self._config_synced = False
        self._bound_client = None
        self._contract = None
        self._binding_epoch = 0
        self._cache = OrderedDict()
        self._scope = None
        self._source_db = None
        self._source_fingerprint = None
    
    def _get_loop(self):
        """获取事件循环"""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.get_event_loop()
    
    async def sync_config_from_db(self, db) -> bool:
        """从数据库同步Embedding配置到EmbeddingClient
        
        解决配置双轨制问题：前端设置保存在数据库，但EmbeddingClient读的是文件配置。
        此方法将数据库中的Embedding配置注入到EmbeddingClient。
        
        Args:
            db: 数据库会话
            
        Returns:
            是否成功同步且配置可用
        """
        client = _embedding_client
        try:
            from app.services.settings_service import SettingsService
            client = _get_embedding_client()
            self._source_db = db
            embedding_config = await SettingsService.get_embedding_config_with_env(db)
            
            api_key = embedding_config.api_key
            api_url = embedding_config.api_url
            model = embedding_config.model
            dimension = embedding_config.dimension

            if db is not None:
                from app.schemas.settings import RAGConfigRequest
                from app.services.settings_service import RAGConfig
                raw_rag = await SettingsService.get_setting(db, SettingsService.KEY_RAG_CONFIG)
                values = json.loads(raw_rag) if raw_rag is not None else {}
                rag = RAGConfigRequest.model_validate({**asdict(RAGConfig()), **values}, strict=True)
                self._contract = make_contract(embedding_config, rag)
                from sqlalchemy import select
                from app.models.knowledge import KnowledgeItem
                rows = (await db.execute(select(KnowledgeItem.id, KnowledgeItem.question, KnowledgeItem.answer).order_by(KnowledgeItem.id))).all()
                digest = hashlib.sha256()
                for row in rows:
                    digest.update(json.dumps(list(row), ensure_ascii=True, separators=(",", ":")).encode())
                    digest.update(b"\n")
                self._source_fingerprint = digest.hexdigest()
            else:
                self._contract = None  # Explicit internal/test callers without a DB cannot attest a generation.
            scope = (api_key, api_url, model, dimension, getattr(embedding_config, "request_dimensions", False),
                     fingerprint(self._contract) if self._contract else None)
            if scope != self._scope:
                self._binding_epoch += 1
                self._cache.clear()
            self._scope = scope
            client.configure(
                api_key=api_key or "",
                api_url=api_url or "",
                model=model or "",
                dimension=dimension or None,
            )
            client._request_dimensions = getattr(embedding_config, "request_dimensions", False)
            client._config_sync_failed = not bool(api_key)
            self._bound_client = client.snapshot()
            self._query_embed_cache.clear()
            
            self._config_synced = bool(api_key)
            logger.info("已同步Embedding配置, model=%s, has_key=%s", 
                       model, bool(api_key))
            return bool(api_key)
        except Exception as e:
            # Revoke old overrides even when a DB read/decryption fails before
            # configure(). None means "leave untouched" in EmbeddingClient.
            if client is not None:
                client._override_api_key = ""
                client._override_api_url = ""
                client._override_model = ""
                client._override_dimension = None
                client._config_sync_failed = True
            self._query_embed_cache.clear()
            self._cache.clear()
            self._binding_epoch += 1
            self._bound_client = None
            self._contract = None
            self._config_synced = False
            logger.error("同步Embedding配置失败: %s", type(e).__name__)
            return False
    
    def _client(self):
        return self._bound_client if self._bound_client is not None else _get_embedding_client()

    async def _run_embedding(self, client, method, data, epoch):
        import time
        from app.core.operation_observation import record_operation
        started = time.perf_counter(); outcome = "degraded"
        try:
            from contextvars import copy_context
            context = copy_context()
            result = await self._get_loop().run_in_executor(_executor, lambda: context.run(method, data))
            if epoch != self._binding_epoch or getattr(client, "_config_sync_failed", False): return None
            outcome = "success" if result is not None else "degraded"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            record_operation("embedding_api", time.perf_counter() - started, outcome)

    async def embed_text(self, text: str) -> Optional[List[float]]:
        client, epoch = self._client(), self._binding_epoch
        if getattr(client, "_config_sync_failed", False): return None
        if text in self._cache:
            self._cache.move_to_end(text)
            return list(self._cache[text])
        try:
            vector = await self._run_embedding(client, client.embed_text, text, epoch)
            if epoch != self._binding_epoch or getattr(client, "_config_sync_failed", False): return None
            if vector is not None:
                self._cache[text] = list(vector)
                if len(self._cache) > 256: self._cache.popitem(last=False)
            return vector
        except Exception as error:
            logger.warning("向量化文本失败: %s", type(error).__name__)
            return None

    async def embed_texts(self, texts: List[str]) -> Optional[List[List[float]]]:
        client, epoch = self._client(), self._binding_epoch
        if getattr(client, "_config_sync_failed", False): return None
        try:
            result = await self._run_embedding(client, client.embed_texts, texts, epoch)
            return None if epoch != self._binding_epoch or getattr(client, "_config_sync_failed", False) else result
        except Exception as error:
            logger.warning("批量向量化失败: %s", type(error).__name__)
            return None

    def compatibility(self):
        info = _get_vector_store().get_index_info()
        expected = fingerprint(self._contract) if self._contract else None
        built = info.get("embedding_contract")
        if not self._config_synced: return False, "Embedding配置不可用"
        if not info["count"]: return False, "向量索引为空"
        if not built: return False, "旧索引没有可核对的Embedding与分块身份，需要显式重建"
        if info.get("knowledge_fingerprint") != self._source_fingerprint:
            return False, "知识正文或条目集合已变化，需要显式重建完整索引"
        if fingerprint(built) != expected: return False, "Embedding地址、模型、维度或分块设置与已建索引不一致"
        return True, "索引身份与当前请求配置匹配"

    @property
    def contract(self):
        return dict(self._contract) if self._contract else None

    async def add_vector(self, item_id: str, vector: List[float]) -> bool:
        """
        添加向量到索引（异步）
        
        Args:
            item_id: 知识条目ID
            vector: 向量
        
        Returns:
            是否成功
        """
        loop = self._get_loop()
        store = _get_vector_store()
        if self._contract is not None and store.count and not self.compatibility()[0]: return False
        contract = self.contract
        return await loop.run_in_executor(_executor, lambda: store.add_vector(item_id, vector, expected_contract=contract))
    
    async def add_item(self, item_id: str, text: str) -> bool:
        """
        添加文本到向量索引（先向量化再添加）
        
        Args:
            item_id: 知识条目ID
            text: 文本内容
        
        Returns:
            是否成功
        """
        vector = await self.embed_text(text)
        if vector is None:
            return False
        return await self.add_vector(item_id, vector)
    
    async def remove_vector(self, item_id: str) -> bool:
        """
        从索引中删除向量（异步）
        
        Args:
            item_id: 知识条目ID
        
        Returns:
            是否成功
        """
        loop = self._get_loop()
        store = _get_vector_store()
        return await loop.run_in_executor(_executor, store.remove_vector, item_id)
    
    async def remove_vectors_by_prefix(self, prefix: str) -> int:
        """
        按前缀删除向量（异步）
        
        Args:
            prefix: ID前缀
        
        Returns:
            删除的数量
        """
        loop = self._get_loop()
        store = _get_vector_store()
        return await loop.run_in_executor(_executor, store.remove_vectors_by_prefix, prefix)
    
    async def search(self, query_vector: List[float], top_k: int = 5) -> List[Tuple[str, float]]:
        """
        搜索相似向量（异步）
        
        Args:
            query_vector: 查询向量
            top_k: 返回数量
        
        Returns:
            [(知识ID, 相似度分数), ...]
        """
        loop = self._get_loop()
        store = _get_vector_store()
        if self._contract is not None and not self.compatibility()[0]: return []
        contract = self.contract
        return await loop.run_in_executor(_executor, lambda: store.search(query_vector, top_k, expected_contract=contract))
    
    async def search_by_text(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """
        通过文本搜索相似内容（异步）
        
        Args:
            query: 查询文本
            top_k: 返回数量
        
        Returns:
            [(知识ID, 相似度分数), ...]
        """
        vector = await self.embed_text(query)
        if vector is None:
            return []
        return await self.search(vector, top_k)
    
    def _embedding_signature(self) -> tuple:
        client = _get_embedding_client()
        # Internal equality only; never log/serialize credentials in this tuple.
        return (client._get_model_name(), client._get_api_url(), client._get_api_key(),
                getattr(client, "_config_revision", 0))

    def begin_rebuild(self) -> dict:
        """Capture before DB reads and embedding collection, not at publication."""
        return {
            "expected_revision": _get_vector_store().revision(),
            "embedding_signature": self._embedding_signature(),
            "embedding_model": self._client()._get_model_name(),
            "embedding_contract": self.contract,
            "knowledge_fingerprint": self._source_fingerprint,
            "config_epoch": __import__("app.core.config", fromlist=["Config"]).Config.embedding_epoch(),
        }

    async def replace_all(self, vectors_data: List[Tuple[str, List[float]]],
                          *, expected_revision: Optional[int] = None,
                          embedding_signature: Optional[tuple] = None,
                          embedding_model: Optional[str] = None,
                          embedding_contract: Optional[dict] = None, config_epoch=None, knowledge_fingerprint=None) -> bool:
        """用给定向量集合原子地替换整个索引（完整构建后再一次性发布）。

        重建期间读者始终看到完整旧索引，不会看到空态或半成品索引；构建在
        锁外进行，不长时间阻塞查询。
        """
        loop = self._get_loop()
        store = _get_vector_store()
        signature = embedding_signature or self._embedding_signature()
        from app.core.config import Config
        if config_epoch is not None and Config.embedding_epoch() != config_epoch: return False
        if self._contract is not None and self._source_db is not None:
            from sqlalchemy.ext.asyncio import AsyncSession
            current = VectorService()
            async with AsyncSession(bind=self._source_db.bind) as validation_db:
                if not await current.sync_config_from_db(validation_db): return False
            if current.contract != embedding_contract or current._scope != self._scope or current._source_fingerprint != knowledge_fingerprint: return False
        return await loop.run_in_executor(
            _executor, lambda: store.rebuild_index(
                vectors_data=vectors_data, expected_revision=expected_revision,
                embedding_model=embedding_model or signature[0], persist=True,
                embedding_contract=embedding_contract, knowledge_fingerprint=knowledge_fingerprint,
                validate_config=lambda: signature == self._embedding_signature() and
                    (config_epoch is None or Config.embedding_epoch() == config_epoch))
        )

    async def save(self) -> bool:
        """保存索引到磁盘（异步）"""
        loop = self._get_loop()
        store = _get_vector_store()
        return await loop.run_in_executor(_executor, store.save)
    
    async def clear(self) -> None:
        """清空索引（异步）"""
        loop = self._get_loop()
        store = _get_vector_store()
        await loop.run_in_executor(_executor, store.clear)
    
    def get_index_info(self) -> Dict[str, Any]:
        """获取索引信息（同步）"""
        store = _get_vector_store()
        return store.get_index_info()
    
    def needs_rebuild(self, current_embedding_model: str = None) -> Tuple[bool, str]:
        """检查是否需要重建索引（同步）"""
        store = _get_vector_store()
        return store.needs_rebuild(current_embedding_model)
    
    def check_dimension_compatibility(self, expected_dimension: int) -> Tuple[bool, str]:
        """检查向量维度兼容性（同步）"""
        store = _get_vector_store()
        return store.check_dimension_compatibility(expected_dimension)
    
    @property
    def count(self) -> int:
        """获取向量数量"""
        store = _get_vector_store()
        return store.count
    
    @property
    def dimension(self) -> int:
        """获取向量维度"""
        store = _get_vector_store()
        return store.dimension
    
    @property
    def embedding_model(self) -> Optional[str]:
        """获取当前embedding模型"""
        store = _get_vector_store()
        return store.embedding_model
    
    def has_item(self, item_id: str) -> bool:
        """检查是否存在某知识的向量"""
        store = _get_vector_store()
        return store.has_item(item_id)
    
    def is_embedding_available(self) -> bool:
        """检查Embedding服务是否可用"""
        client = self._client()
        return not getattr(client, "_config_sync_failed", False) and client.is_available()
    
    @property
    def embedding_dimension(self) -> int:
        """获取Embedding维度"""
        client = self._client()
        return client.dimension


class EmbeddingService:
    """Embedding服务类 - 异步封装"""
    
    async def embed_text(self, text: str) -> Optional[List[float]]:
        """将文本向量化（异步）"""
        loop = asyncio.get_running_loop()
        client = _get_embedding_client()
        if getattr(client, "_config_sync_failed", False):
            return None
        result = await loop.run_in_executor(_executor, client.embed_text, text)
        return None if getattr(client, "_config_sync_failed", False) else result
    
    async def embed_texts(self, texts: List[str]) -> Optional[List[List[float]]]:
        """批量将文本向量化（异步）"""
        loop = asyncio.get_running_loop()
        client = _get_embedding_client()
        if getattr(client, "_config_sync_failed", False):
            return None
        result = await loop.run_in_executor(_executor, client.embed_texts, texts)
        return None if getattr(client, "_config_sync_failed", False) else result
    
    def is_available(self) -> bool:
        """检查服务是否可用"""
        client = _get_embedding_client()
        return not getattr(client, "_config_sync_failed", False) and client.is_available()
    
    @property
    def dimension(self) -> int:
        """获取向量维度"""
        client = _get_embedding_client()
        return client.dimension
