# -*- coding: utf-8 -*-
"""
知识库服务模块 - Knowledge Service

功能:
- 知识库CRUD操作
- 文本分块（Chunking）+ 向量索引同步
- 重复检测
- 导入导出
"""

import uuid
import logging
import copy
import functools
import inspect
from contextvars import ContextVar
from typing import List, Optional, Tuple, Dict, Any

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeItem
from app.services.vector_service import VectorService
from app.core.config import Config
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


def generate_knowledge_id() -> str:
    """生成唯一知识条目ID"""
    return f"kb_{uuid.uuid4().hex[:12]}"


# ==================== 分块工具函数 ====================

CHUNK_ID_SEP = "#chunk_"


def make_chunk_id(knowledge_id: str, chunk_index: int) -> str:
    """生成 chunk 级向量 ID，如 kb_xxxx#chunk_0"""
    return f"{knowledge_id}{CHUNK_ID_SEP}{chunk_index}"


def split_chunk_id(chunk_id: str) -> Tuple[str, int]:
    """将 chunk ID 解析回 (knowledge_id, chunk_index)，
    如果不含分隔符则视为整条知识（chunk_index = -1）。
    """
    if CHUNK_ID_SEP in chunk_id:
        parts = chunk_id.rsplit(CHUNK_ID_SEP, 1)
        try:
            return parts[0], int(parts[1])
        except (ValueError, IndexError):
            return chunk_id, -1
    return chunk_id, -1


def chunk_text(text: str, chunk_size: int = 500, chunk_overlap: int = 50) -> List[str]:
    """按字符长度切片 + overlap 进行文本分块。

    Args:
        text: 待分块文本
        chunk_size: 每块最大字符数
        chunk_overlap: 相邻块重叠字符数

    Returns:
        分块列表（至少返回一个块）
    """
    if not text:
        return [""]
    if len(text) <= chunk_size:
        return [text]

    chunks: List[str] = []
    start = 0
    step = max(1, chunk_size - chunk_overlap)
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start += step
    return chunks


_vector_context = ContextVar("knowledge_vectors", default=None)
_chunk_context = ContextVar("knowledge_chunks", default=None)


def bound_vectors(method):
    @functools.wraps(method)
    async def wrapped(self, *args, **kwargs):
        arguments = inspect.signature(method).bind(self, *args, **kwargs).arguments
        db = arguments.get("db")
        if db is None: return await method(self, *args, **kwargs)
        vector = copy.copy(self.vector_service)
        vector._cache = type(vector._cache)()
        vector._bound_client = None
        vector._contract = None
        if not await vector.sync_config_from_db(db):
            return (0, ["Embedding configuration unavailable; previous generation retained"]) if method.__name__ == "rebuild_index" else False
        contract = vector.contract
        geometry = (contract["chunk_size"], contract["chunk_overlap"], contract["chunk_max_per_item"]) if contract else self._get_chunk_config()
        token = _vector_context.set(vector); chunks = _chunk_context.set(geometry)
        try: return await method(self, *args, **kwargs)
        finally: _chunk_context.reset(chunks); _vector_context.reset(token)
    return wrapped


class KnowledgeService:
    """知识库服务类"""

    def __init__(self):
        self._vector_service = VectorService()
        self._config = Config()

    @property
    def vector_service(self):
        return _vector_context.get() or self._vector_service

    @vector_service.setter
    def vector_service(self, value):
        self._vector_service = value

    # ==================== 分块配置 ====================

    def _get_chunk_config(self) -> Tuple[int, int, int]:
        """返回 (chunk_size, chunk_overlap, chunk_max_per_item)"""
        if _chunk_context.get() is not None: return _chunk_context.get()
        cfg = self._config
        return (
            int(cfg.get("chunk_size", 500)),
            int(cfg.get("chunk_overlap", 50)),
            int(cfg.get("chunk_max_per_item", 10)),
        )

    # ==================== CRUD ====================

    async def create_knowledge(
        self,
        db: AsyncSession,
        question: str,
        answer: str,
        keywords: Optional[List[str]] = None,
        category: str = "通用",
        score: float = 1.0,
        sync_vector: bool = True,
        knowledge_id: Optional[str] = None,
        crop: Optional[str] = None,
        region: Optional[str] = None,
        growth_stage: Optional[str] = None,
        topic: Optional[str] = None,
        disease: Optional[str] = None,
        source_type: Optional[str] = "unverified",
        source_name: Optional[str] = None,
        authority_level: Optional[str] = "未核验",
        version: Optional[str] = None,
        publish_date: Optional[str] = None,
        url: Optional[str] = None,
        source_updated_at: Optional[str] = None,
        effective_from: Optional[str] = None,
        effective_to: Optional[str] = None,
        is_current: Optional[int] = 1,
        meta_info: Optional[Dict[str, Any]] = None,
    ) -> KnowledgeItem:
        from app.core.knowledge_contract import validate_effective_dates
        validate_effective_dates(effective_from, effective_to)
        knowledge_id = knowledge_id or generate_knowledge_id()

        # 如果指定了 ID 且已存在，先删除旧条目（用于商品知识同步的幂等写入）
        if knowledge_id:
            existing = await self.get_knowledge(db, knowledge_id)
            if existing:
                await self.delete_knowledge(db, knowledge_id, sync_vector=sync_vector)

        item = KnowledgeItem(
            id=knowledge_id,
            question=question,
            answer=answer,
            keywords=keywords or [],
            category=category,
            score=score,
            crop=crop,
            region=region,
            growth_stage=growth_stage,
            topic=topic,
            disease=disease,
            source_type=(source_type or '').strip() or "unverified",
            source_name=source_name,
            authority_level=(authority_level or '').strip() or "未核验",
            version=(version or '').strip() or None,
            publish_date=publish_date,
            url=url,
            source_updated_at=source_updated_at,
            effective_from=effective_from,
            effective_to=effective_to,
            is_current=is_current if is_current is not None else 0,
            meta_info=copy.deepcopy(meta_info or {}),
            created_at=utcnow(),
            updated_at=utcnow()
        )

        db.add(item)
        await db.commit()
        await db.refresh(item)

        if sync_vector:
            await self._sync_vector_add(item, db=db)

        logger.info(f"创建知识条目: {knowledge_id}")
        return item

    async def get_knowledge(
        self,
        db: AsyncSession,
        knowledge_id: str
    ) -> Optional[KnowledgeItem]:
        stmt = select(KnowledgeItem).where(KnowledgeItem.id == knowledge_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_knowledge_list(
        self,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        category: Optional[str] = None,
        keyword: Optional[str] = None,
        crop: Optional[str] = None,
        growth_stage: Optional[str] = None,
        topic: Optional[str] = None,
        source_type: Optional[str] = None,
        region: Optional[str] = None,
        disease: Optional[str] = None
    ) -> Tuple[List[KnowledgeItem], int]:
        stmt = select(KnowledgeItem)
        count_stmt = select(func.count(KnowledgeItem.id))

        if category:
            stmt = stmt.where(KnowledgeItem.category == category)
            count_stmt = count_stmt.where(KnowledgeItem.category == category)

        from app.core.knowledge_contract import metadata_predicates
        clauses = metadata_predicates(KnowledgeItem, dict(crop=crop, growth_stage=growth_stage,
            topic=topic, region=region, disease=disease))
        stmt = stmt.where(*clauses)
        count_stmt = count_stmt.where(*clauses)

        if source_type:
            stmt = stmt.where(KnowledgeItem.source_type == source_type)
            count_stmt = count_stmt.where(KnowledgeItem.source_type == source_type)

        if keyword:
            search_pattern = f"%{keyword}%"
            keyword_filter = or_(
                KnowledgeItem.question.ilike(search_pattern),
                KnowledgeItem.answer.ilike(search_pattern)
            )
            stmt = stmt.where(keyword_filter)
            count_stmt = count_stmt.where(keyword_filter)

        stmt = stmt.order_by(KnowledgeItem.updated_at.desc(), KnowledgeItem.id)
        offset = (page - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size)

        result = await db.execute(stmt)
        items = list(result.scalars().all())

        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0

        return items, total

    async def filter_candidate_ids(
        self,
        db: AsyncSession,
        crop: Optional[str] = None,
        growth_stage: Optional[str] = None,
        topic: Optional[str] = None,
        region: Optional[str] = None,
        disease: Optional[str] = None
    ) -> List[str]:
        """按农业元数据前置过滤命中条目 ID 集合"""
        from app.core.knowledge_contract import metadata_predicates, candidate_eligible
        from app.core.weather_policy import agriculture_today
        stmt = select(KnowledgeItem).where(KnowledgeItem.is_current == 1, *metadata_predicates(KnowledgeItem,
            dict(crop=crop, growth_stage=growth_stage, topic=topic, region=region, disease=disease)))
        result = await db.execute(stmt)
        return [row.id for row in result.scalars().all() if candidate_eligible(row, agriculture_today())]

    async def update_knowledge(
        self,
        db: AsyncSession,
        knowledge_id: str,
        question: Optional[str] = None,
        answer: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        category: Optional[str] = None,
        score: Optional[float] = None,
        crop: Optional[str] = None,
        region: Optional[str] = None,
        growth_stage: Optional[str] = None,
        topic: Optional[str] = None,
        disease: Optional[str] = None,
        source_type: Optional[str] = None,
        source_name: Optional[str] = None,
        authority_level: Optional[str] = None,
        version: Optional[str] = None,
        publish_date: Optional[str] = None,
        url: Optional[str] = None,
        source_updated_at: Optional[str] = None,
        is_current: Optional[int] = None,
        effective_from: Optional[str] = None,
        effective_to: Optional[str] = None,
        sync_vector: bool = True
    ) -> Optional[KnowledgeItem]:
        item = await self.get_knowledge(db, knowledge_id)
        if not item:
            return None

        from app.core.knowledge_contract import validate_effective_dates
        validate_effective_dates(effective_from if effective_from is not None else item.effective_from,
                                 effective_to if effective_to is not None else item.effective_to)
        need_vector_update = False

        if question is not None:
            item.question = question
            need_vector_update = True
        if answer is not None:
            item.answer = answer
            need_vector_update = True
        if keywords is not None:
            item.keywords = keywords
        if category is not None:
            item.category = category
        if score is not None:
            item.score = score
        if crop is not None:
            item.crop = crop
        if region is not None:
            item.region = region
        if growth_stage is not None:
            item.growth_stage = growth_stage
        if topic is not None:
            item.topic = topic
        if disease is not None:
            item.disease = disease
        if source_type is not None:
            item.source_type = source_type.strip() or 'unverified'
        if source_name is not None:
            item.source_name = source_name
        if authority_level is not None:
            item.authority_level = authority_level.strip() or '未核验'
        if version is not None:
            item.version = version.strip() or None
        if publish_date is not None:
            item.publish_date = publish_date
        if url is not None:
            item.url = url
        if source_updated_at is not None:
            item.source_updated_at = source_updated_at
        if is_current is not None:
            item.is_current = is_current
        if effective_from is not None:
            item.effective_from = effective_from
        if effective_to is not None:
            item.effective_to = effective_to

        item.updated_at = utcnow()

        await db.commit()
        await db.refresh(item)

        if sync_vector and need_vector_update:
            await self._sync_vector_update(item, db=db)

        logger.info(f"更新知识条目: {knowledge_id}")
        return item

    async def delete_knowledge(
        self,
        db: AsyncSession,
        knowledge_id: str,
        sync_vector: bool = True
    ) -> bool:
        item = await self.get_knowledge(db, knowledge_id)
        if not item:
            return False

        await db.delete(item)
        await db.commit()

        if sync_vector:
            await self._sync_vector_remove(knowledge_id)

        logger.info(f"删除知识条目: {knowledge_id}")
        return True

    async def check_duplicate(
        self,
        db: AsyncSession,
        question: str,
        exclude_id: Optional[str] = None
    ) -> Optional[KnowledgeItem]:
        stmt = select(KnowledgeItem).where(KnowledgeItem.question == question)
        if exclude_id:
            stmt = stmt.where(KnowledgeItem.id != exclude_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_categories(self, db: AsyncSession) -> List[str]:
        stmt = select(KnowledgeItem.category).distinct()
        result = await db.execute(stmt)
        return [row[0] for row in result.fetchall()]

    async def get_total_count(self, db: AsyncSession) -> int:
        stmt = select(func.count(KnowledgeItem.id))
        result = await db.execute(stmt)
        return result.scalar() or 0
    async def delete_by_category(self, db: AsyncSession, category: str) -> int:
        """删除指定分类下的所有知识条目，同步删除向量索引。

        Returns:
            删除的条目数
        """
        stmt = select(KnowledgeItem).where(KnowledgeItem.category == category)
        result = await db.execute(stmt)
        items = list(result.scalars().all())

        count = 0
        for item in items:
            await self._sync_vector_remove(item.id, persist_index=False)
            await db.delete(item)
            count += 1

        if count:
            await db.commit()
            await self.vector_service.save()
            logger.info(f"按分类删除知识条目: category={category}, count={count}")

        return count

    @staticmethod
    async def get_all_knowledge_items(db: AsyncSession) -> List[KnowledgeItem]:
        stmt = select(KnowledgeItem)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # ==================== 导入导出 ====================

    async def export_all(self, db: AsyncSession) -> List[Dict[str, Any]]:
        stmt = select(KnowledgeItem).order_by(KnowledgeItem.created_at.asc())
        result = await db.execute(stmt)
        items = result.scalars().all()

        from datetime import datetime
        return [{column.name: (value.isoformat() if isinstance(value, datetime) else value)
            for column in KnowledgeItem.__table__.columns
            for value in [getattr(item, column.name)]} for item in items]

    async def lifecycle_summary(self, db: AsyncSession) -> Dict[str, Any]:
        """Report recorded metadata/candidates, without inventing a global approved version."""
        from app.core.knowledge_contract import candidate_eligible, source_quarantined, effective_on
        from app.core.weather_policy import agriculture_today
        from app.core.schema_revision import CURRENT_SCHEMA_REVISION
        columns = ('id', 'crop', 'version', 'is_current', 'effective_from', 'effective_to', 'source_type', 'source_name')
        result = await db.execute(select(*(getattr(KnowledgeItem, key) for key in columns)))
        rows = [dict(row) for row in result.mappings().all()]
        today = agriculture_today()
        current = [row for row in rows if row['is_current'] == 1]
        eligible = [row for row in current if candidate_eligible(row, today)]
        crops = sorted({row['crop'].strip() for row in eligible if row['crop'] and row['crop'].strip()})
        return dict(current_version=None, schema_version=CURRENT_SCHEMA_REVISION,
            status='candidates_available' if eligible else 'no_eligible_candidates' if rows else 'empty',
            total_items=len(rows), current_items=len(current), candidate_items=len(eligible),
            excluded_by_source=sum(source_quarantined(row) for row in current),
            excluded_by_dates=sum(not effective_on(row, today) for row in current),
            recorded_versions=sorted({row['version'].strip() for row in rows if row['version'] and row['version'].strip()}),
            candidate_versions=sorted({row['version'].strip() for row in eligible if row['version'] and row['version'].strip()}),
            supported_crops=crops, recorded_candidate_crops=crops, eligibility_date=today.isoformat(),
            note='来源、版本和作物仅为数据库标注；排除原因可能重叠，不证明权威性、适用性或已获作业许可。supported_crops为候选作物标注的兼容字段。')

    async def import_items(
        self,
        db: AsyncSession,
        items: List[Dict[str, Any]],
        skip_duplicates: bool = True,
        sync_vector: bool = True
    ) -> Tuple[int, int, List[str]]:
        success_count = 0
        skip_count = 0
        errors = []

        for idx, item_data in enumerate(items):
            try:
                from app.schemas.knowledge import ImportKnowledgeItem
                validated = ImportKnowledgeItem.model_validate(item_data)
                question = validated.question.strip()
                answer = validated.answer.strip()

                if not question or not answer:
                    errors.append(f"第{idx + 1}条: 问题或答案为空")
                    continue

                if skip_duplicates:
                    duplicate = await self.check_duplicate(db, question)
                    if duplicate:
                        skip_count += 1
                        continue

                # Ignore export identities: imports create new rows, never replace installed data.
                values = validated.model_dump(exclude={'id', 'created_at', 'updated_at'})
                values.update(question=question, answer=answer)
                await self.create_knowledge(db=db, sync_vector=sync_vector, **values)
                success_count += 1

            except Exception as error:
                from fastapi import HTTPException
                from pydantic import ValidationError
                await db.rollback()
                if isinstance(error, HTTPException):
                    detail = error.detail
                elif isinstance(error, ValidationError):
                    fields = sorted({str(issue['loc'][0]) for issue in error.errors() if issue['loc']})
                    detail = '字段校验失败：' + ','.join(fields)
                else:
                    detail = '保存失败，请核对该条记录后重试'
                errors.append(f"第{idx + 1}条: {detail}")
                logger.warning('knowledge_import_failed row=%s error_type=%s', idx + 1, type(error).__name__)

        return success_count, skip_count, errors

    # ==================== 向量索引管理（分块） ====================

    @staticmethod
    def _complete_chunk_vectors(chunks, embeddings):
        """A document is usable only if every requested chunk has a vector."""
        if (not chunks or not isinstance(embeddings, (list, tuple))
                or len(embeddings) != len(chunks) or any(v is None for v in embeddings)):
            raise ValueError("Incomplete embedding response; previous generation retained")
        return [(chunk_id, vector) for (chunk_id, _), vector in zip(chunks, embeddings)]

    def _build_chunks(self, item: KnowledgeItem) -> List[Tuple[str, str]]:
        """将一条知识拆分为多个 chunk，返回 [(chunk_id, chunk_text), ...]。

        短文本（不超过 chunk_size）仍然只产生一个 chunk。
        """
        chunk_size, chunk_overlap, chunk_max = self._get_chunk_config()

        full_text = f"{item.question}\n{item.answer}"
        chunks = chunk_text(full_text, chunk_size, chunk_overlap)

        # 限制每条知识最多 chunk_max 个 chunk
        chunks = chunks[:chunk_max]

        return [
            (make_chunk_id(item.id, idx), text)
            for idx, text in enumerate(chunks)
        ]

    @bound_vectors
    async def _sync_vector_add(
        self,
        item: KnowledgeItem,
        db: AsyncSession = None,
        persist_index: bool = True
    ) -> bool:
        """添加知识条目的所有 chunk 向量到索引"""
        from app.services.performance_service import get_performance_service
        import time as _time
        perf = get_performance_service()
        start = _time.perf_counter()
        success = True
        try:
            if db and not self.vector_service._config_synced:
                await self.vector_service.sync_config_from_db(db)

            if self.vector_service.contract is not None:
                return False  # SQL remains authoritative; only a complete explicit rebuild attests the whole collection.
            chunks = self._build_chunks(item)
            any_added = False

            # 批量 embedding
            texts = [text for _, text in chunks]
            embeddings = await self.vector_service.embed_texts(texts)

            if embeddings is None:
                success = False
                return False

            for (chunk_id, _), vector in zip(chunks, embeddings):
                if vector is None:
                    continue
                added = await self.vector_service.add_vector(chunk_id, vector)
                if added:
                    any_added = True

            if any_added and persist_index:
                if not await self.vector_service.save():
                    success = False
                    return False
            if not any_added:
                success = False
            return any_added
        except Exception as e:
            success = False
            logger.error(f"添加向量索引失败: {item.id}, {e}")
            return False
        finally:
            duration = _time.perf_counter() - start
            perf.record("knowledge_add", duration, success)

    @bound_vectors
    async def _sync_vector_update(
        self,
        item: KnowledgeItem,
        db: AsyncSession = None,
        persist_index: bool = True
    ) -> bool:
        """更新知识条目的向量索引：先删除旧 chunk，再添加新 chunk"""
        from app.services.performance_service import get_performance_service
        import time as _time
        perf = get_performance_service()
        start = _time.perf_counter()
        success = True
        try:
            if db and not self.vector_service._config_synced:
                await self.vector_service.sync_config_from_db(db)

            if self.vector_service.contract is not None:
                return False
            # 按前缀删除该知识的所有 chunk 向量
            prefix = f"{item.id}{CHUNK_ID_SEP}"
            await self.vector_service.remove_vectors_by_prefix(prefix)
            # 也删除可能存在的旧格式（无 chunk 后缀）向量
            await self.vector_service.remove_vector(item.id)

            # 重新添加
            chunks = self._build_chunks(item)
            texts = [text for _, text in chunks]
            embeddings = await self.vector_service.embed_texts(texts)

            any_added = False
            if embeddings:
                for (chunk_id, _), vector in zip(chunks, embeddings):
                    if vector is None:
                        continue
                    added = await self.vector_service.add_vector(chunk_id, vector)
                    if added:
                        any_added = True

            if persist_index and not await self.vector_service.save():
                success = False
                return False
            if not any_added:
                success = False
            return any_added
        except Exception as e:
            success = False
            logger.error(f"更新向量索引失败: {item.id}, {e}")
            return False
        finally:
            duration = _time.perf_counter() - start
            perf.record("knowledge_update", duration, success)

    async def _sync_vector_remove(self, knowledge_id: str, persist_index: bool = True) -> bool:
        """从向量索引中删除知识条目的所有 chunk 向量"""
        try:
            # 删除 chunk 格式的向量
            prefix = f"{knowledge_id}{CHUNK_ID_SEP}"
            removed_count = await self.vector_service.remove_vectors_by_prefix(prefix)
            # 也删除可能存在的旧格式（无 chunk 后缀）向量
            removed_old = await self.vector_service.remove_vector(knowledge_id)
            removed = removed_count > 0 or removed_old
            if removed and persist_index and not await self.vector_service.save():
                return False
            return removed
        except Exception as e:
            logger.error(f"删除向量索引失败: {knowledge_id}, {e}")
            return False

    @bound_vectors
    async def rebuild_index(self, db: AsyncSession) -> Tuple[int, List[str]]:
        """重建向量索引（分块模式）。

        先在锁外把所有 chunk 向量化并收集，最后一次原子替换整个索引——
        重建期间读者始终看到完整旧索引，不会看到空态或半成品。
        """
        config_ok = await self.vector_service.sync_config_from_db(db)
        if not config_ok:
            return 0, ["Embedding configuration unavailable; previous generation retained"]

        rebuild_context = self.vector_service.begin_rebuild()
        stmt = select(KnowledgeItem)
        result = await db.execute(stmt)
        items = result.scalars().all()

        success_count = 0
        errors: List[str] = []
        collected: List[Tuple[str, List[float]]] = []

        for item in items:
            try:
                chunks = self._build_chunks(item)
                embeddings = await self.vector_service.embed_texts([text for _, text in chunks])
                collected.extend(self._complete_chunk_vectors(chunks, embeddings))
                success_count += 1
            except Exception as e:
                errors.append(f"{item.id}: {str(e)}")

        if errors:
            return 0, errors
        if not await self.vector_service.replace_all(collected, **rebuild_context):
            return 0, ["Candidate publication failed or concurrent write/config change; previous generation retained"]

        logger.info(f"重建向量索引完成: 成功 {success_count}, 失败 {len(errors)}")
        return success_count, errors

    def get_index_status(self) -> Dict[str, Any]:
        return {
            "count": self.vector_service.count,
            "dimension": self.vector_service.dimension,
            "embedding_model": self.vector_service.embedding_model,
            "embedding_available": self.vector_service.is_embedding_available(),
            "embedding_dimension": self.vector_service.embedding_dimension if self.vector_service.is_embedding_available() else None
        }

    def check_index_needs_rebuild(self, current_model: Optional[str] = None) -> Tuple[bool, str]:
        return self.vector_service.needs_rebuild(current_model)


# 创建全局服务实例
knowledge_service = KnowledgeService()
