# -*- coding: utf-8 -*-
"""
Embedding模块 - 调用硅基流动API进行文本向量化

优化内容 (v2.2.0):
- 细化异常处理
- 添加指数退避重试机制
- 集成API限流保护
- 批处理失败时继续处理其他批次

优化内容 (v2.3.0):
- 支持外部配置注入（数据库配置优先于文件配置）
- 统一API密钥/模型/地址的获取逻辑
"""

import time
import requests
import logging
import math
from typing import List, Optional
from requests.exceptions import RequestException, Timeout, ConnectionError
from app.core.config import Config


logger = logging.getLogger(__name__)


def exponential_backoff(attempt: int, base_delay: float = 1.0) -> float:
    """计算指数退避延迟时间"""
    return min(base_delay * (2 ** attempt), 30.0)


class EmbeddingClient:
    """Embedding客户端 - 支持硅基流动API"""
    
    _instance = None
    
    # 支持的Embedding模型映射
    MODEL_MAPPING = {
        "bge-large-zh": "BAAI/bge-large-zh-v1.5",
        "m3e-base": "BAAI/bge-m3",
        "text-embedding-ada-002": "text-embedding-ada-002"
    }
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.config = Config()
        self._dimension = 1024  # bge-large-zh-v1.5 维度
        
        # 外部注入的配置（优先级高于默认配置）
        self._override_api_key: Optional[str] = None
        self._override_api_url: Optional[str] = None
        self._override_model: Optional[str] = None
        self._override_dimension: Optional[int] = None
        self._config_revision = 0
        self._request_dimensions = False
        
        # 重试配置
        self._max_retries = 3
        self._base_delay = 1.0
    
    def snapshot(self):
        frozen = object.__new__(EmbeddingClient)
        frozen.__dict__.update(self.__dict__)
        return frozen

    def configure(self, api_key: str = None, api_url: str = None, 
                  model: str = None, dimension: int = None):
        """注入外部配置（来自数据库），优先级高于默认值和环境变量
        
        Args:
            api_key: API密钥
            api_url: API地址
            model: 模型名称（如 BAAI/bge-large-zh-v1.5）
            dimension: 向量维度
        """
        previous = (self._override_api_key, self._override_api_url,
                    self._override_model, self._override_dimension)
        if api_key is not None:
            self._override_api_key = api_key
        if api_url is not None:
            self._override_api_url = api_url
        if model is not None:
            self._override_model = model
        if dimension is not None:
            self._override_dimension = dimension
            self._dimension = dimension
        current = (self._override_api_key, self._override_api_url,
                   self._override_model, self._override_dimension)
        if current != previous:
            self._config_revision = getattr(self, "_config_revision", 0) + 1
    
    def _get_api_key(self) -> str:
        """获取API密钥（优先使用外部注入的配置）"""
        if self._override_api_key is not None:
            return self._override_api_key
        return self.config.get("api_key", "")
    
    def _get_api_url(self) -> str:
        """获取API地址（优先使用外部注入的配置）"""
        if self._override_api_url is not None:
            return self._override_api_url
        return self.config.get("api_base_url", "") or "https://api.siliconflow.cn/v1"
    
    @property
    def dimension(self) -> int:
        """获取向量维度"""
        return self._dimension
    
    def _get_model_name(self) -> str:
        """获取实际的模型名称"""
        if self._override_model is not None:
            return self._override_model
        ui_model = self.config.get("embedding_model", "bge-large-zh")
        return self.MODEL_MAPPING.get(ui_model, ui_model)
    
    def embed_text(self, text: str) -> Optional[List[float]]:
        """
        将单个文本向量化
        
        Args:
            text: 要向量化的文本
        
        Returns:
            向量列表，失败返回None
        """
        result = self.embed_texts([text])
        if result and len(result) > 0:
            return result[0]
        return None
    
    def embed_texts(self, texts: List[str]) -> Optional[List[List[float]]]:
        """
        批量将文本向量化（自动分批处理，失败时继续处理其他批次）
        
        Args:
            texts: 要向量化的文本列表
        
        Returns:
            向量列表的列表，部分失败时对应位置为None
        """
        if not texts:
            return []
        
        api_key = self._get_api_key()
        if not api_key:
            logger.warning("未配置API密钥，无法进行向量化")
            return None
        
        # API最大批次大小
        BATCH_SIZE = 32
        
        # 如果数量小于批次大小，直接调用
        if len(texts) <= BATCH_SIZE:
            return self._embed_batch_with_retry(texts)
        
        # 分批处理
        all_embeddings: List[Optional[List[float]]] = [None] * len(texts)
        total_batches = (len(texts) + BATCH_SIZE - 1) // BATCH_SIZE
        success_count = 0
        fail_count = 0
        
        logger.info("共 %s 条文本，分 %s 批处理（每批最多 %s 条）", len(texts), total_batches, BATCH_SIZE)
        
        for batch_idx in range(total_batches):
            start_idx = batch_idx * BATCH_SIZE
            end_idx = min(start_idx + BATCH_SIZE, len(texts))
            batch = texts[start_idx:end_idx]
            batch_num = batch_idx + 1
            
            logger.info("处理批次 %s/%s", batch_num, total_batches)
            
            embeddings = self._embed_batch_with_retry(batch)
            
            if embeddings is None:
                logger.warning("批次 %s 失败，跳过继续处理", batch_num)
                fail_count += len(batch)
            else:
                for i, emb in enumerate(embeddings):
                    all_embeddings[start_idx + i] = emb
                success_count += len(embeddings)
        
        logger.info("向量化完成: 成功 %s, 失败 %s", success_count, fail_count)
        
        if success_count == 0:
            return None
        
        return all_embeddings
    
    def _embed_batch_with_retry(self, texts: List[str]) -> Optional[List[List[float]]]:
        """带重试的批量向量化"""
        last_error = None
        
        for attempt in range(self._max_retries):
            try:
                result = self._embed_batch(texts)
                return result  # Permanent rejection is not a reason to repeat a paid request.
            except Exception as e:
                if isinstance(e, (ValueError, TypeError, KeyError, OverflowError)):
                    logger.warning("Embedding响应或配置结构无效，本次不重试")
                    return None
                last_error = e
                logger.warning("Embedding批次处理失败（尝试 %d/%d）: %s", 
                             attempt + 1, self._max_retries, str(e))
            
            if attempt < self._max_retries - 1:
                delay = exponential_backoff(attempt, self._base_delay)
                time.sleep(delay)
        
        if last_error:
            logger.error("Embedding批次处理最终失败: %s", str(last_error))
        return None
    
    def _embed_batch(self, texts: List[str]) -> Optional[List[List[float]]]:
        """处理单个批次的embedding"""
        api_key = self._get_api_key()
        api_base_url = self._get_api_url()
        model = self._get_model_name()

        # 使用侧 SSRF 校验：外呼前拒绝不安全地址（含继承 LLM、provider/模型切换或旧库中的不安全 URL）
        from app.utils.security import validate_safe_external_url
        is_safe, err_msg = validate_safe_external_url(api_base_url)
        if not is_safe:
            logger.error("Embedding API 地址不安全，已拒绝外呼: %s", err_msg)
            return None

        url = f"{api_base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model,
            "input": texts,
            "encoding_format": "float"
        }
        
        if self._request_dimensions and self._override_dimension is not None:
            payload["dimensions"] = self._override_dimension

        try:
            # A safe initial host does not make a redirect destination safe. Never
            # follow redirects (including method-preserving 307/308) with credentials.
            from app.core.operation_observation import observe_operation
            with observe_operation("embedding_http") as measurement:
                response = requests.post(
                    url, headers=headers, json=payload, timeout=60, allow_redirects=False
                )
                measurement.outcome = "success" if response.status_code == 200 else "error"
            
            if response.status_code == 200:
                result = response.json()
                rows = result.get("data") if isinstance(result, dict) else None
                if not isinstance(rows, list) or len(rows) != len(texts): return None
                ordered = [None] * len(texts)
                expected = self._override_dimension
                for row in rows:
                    index = row.get("index") if isinstance(row, dict) else None
                    vector = row.get("embedding") if isinstance(row, dict) else None
                    if type(index) is not int or not 0 <= index < len(texts) or ordered[index] is not None: return None
                    if (not isinstance(vector, list) or not vector or expected is not None and len(vector) != expected
                            or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)):
                        return None
                    ordered[index] = vector
                if len({len(vector) for vector in ordered}) != 1: return None
                if expected is None: self._dimension = len(ordered[0])
                return ordered
            elif response.status_code == 401:
                logger.error("Embedding API密钥无效")
                return None
            elif response.status_code == 429:
                logger.warning("Embedding API限流，稍后重试")
                raise Exception("API限流")
            elif response.status_code >= 500:
                logger.warning("Embedding API服务器错误: %s", response.status_code)
                raise Exception(f"服务器错误 {response.status_code}")
            else:
                logger.warning("Embedding API调用失败: HTTP %s", response.status_code)
                return None
        except Timeout:
            logger.warning("Embedding API调用超时")
            raise Exception("请求超时")
        except ConnectionError:
            logger.warning("Embedding API网络连接失败")
            raise Exception("网络连接失败")
        except RequestException as e:
            logger.warning("Embedding API网络错误: %s", type(e).__name__)
            raise Exception("Embedding网络错误") from None
    
    def is_available(self) -> bool:
        """检查Embedding服务是否可用"""
        api_key = self._get_api_key()
        return bool(api_key)
