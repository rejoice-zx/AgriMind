"""Agricultural retrieval reads persisted settings and eligible current rows per request."""
import hashlib
import json
from dataclasses import asdict
from datetime import date
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, func
from app.models.knowledge import KnowledgeItem
from app.schemas.settings import RAGConfigRequest
from app.services.settings_service import SettingsService, RAGConfig
from app.services.rag_service import RAGService, QueryUnderstanding
from app.core.weather_policy import agriculture_today, finite_number
from app.core.knowledge_contract import effective_on


class AgriRetrievalService:
    @staticmethod
    async def config(db):
        raw = await SettingsService.get_setting(db, SettingsService.KEY_RAG_CONFIG)
        origin = "saved" if raw is not None else "defaults"
        try:
            values = json.loads(raw) if raw is not None else {}
            if not isinstance(values, dict): raise ValueError("invalid object")
            config = RAGConfigRequest.model_validate({**asdict(RAGConfig()), **values}, strict=True)
        except (ValueError, TypeError, ValidationError):
            raise HTTPException(503, "已保存的RAG配置无效，请管理员核对；本次未使用默认值替代检索") from None
        return config, origin

    effective_on = staticmethod(effective_on)

    @classmethod
    async def search(cls, db, query, metadata=None, top_k=None, threshold=None):
        config, origin = await cls.config(db)
        if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20):
            raise HTTPException(422, "top_k必须是1至20的整数")
        if threshold is not None and finite_number(threshold, 0, 1) is None:
            raise HTTPException(422, "检索融合得分门限必须是0至1的有限数值")
        effective_k = min(top_k, config.top_k) if top_k is not None else config.top_k
        effective_threshold = float(threshold) if threshold is not None else config.similarity_threshold
        filters = dict(metadata or {})
        if not filters.get("crop"):
            crop = QueryUnderstanding.extract_entities(query).get("crop")
            if crop: filters["crop"] = crop
        stmt = select(KnowledgeItem).where(KnowledgeItem.is_current == 1)
        for key, value in filters.items():
            if key not in {"crop", "growth_stage", "topic", "disease", "region"}:
                raise HTTPException(422, "不支持的农业知识过滤字段")
            if value:
                if not isinstance(value, str): raise HTTPException(422, "农业知识过滤值必须是文本")
                # Crop remains a strict SQL boundary; controlled aliases are evaluated below.
                if key == 'crop': stmt = stmt.where(func.lower(KnowledgeItem.crop) == value.strip().lower())
        today = agriculture_today()
        rows = (await db.scalars(stmt.order_by(KnowledgeItem.id))).all()
        from app.core.knowledge_contract import candidate_eligible, metadata_matches
        rows = [row for row in rows if all(not value or metadata_matches(row, key, value, filters.get('crop')) for key, value in filters.items())]
        dated = [row for row in rows if cls.effective_on(row, today)]
        eligible = [row for row in dated if candidate_eligible(row, today)]
        columns = ("id", "question", "answer", "category", "crop", "region", "growth_stage", "topic", "disease", "is_current",
                   "source_type", "source_name", "authority_level", "version", "publish_date", "url", "effective_from", "effective_to")
        candidates = [{**{key: getattr(row, key) for key in columns},
                       "authority_level": row.authority_level or "未标注",
                       "keywords": [word for word in (row.keywords if isinstance(row.keywords, list) else []) if isinstance(word, str)]} for row in eligible]
        rag = RAGService(similarity_threshold=effective_threshold, retrieval_top_k=effective_k,
            context_max_chars=config.max_context_length, context_top_n=effective_k,
            use_rewrite=config.use_rewrite, source_labels=True)
        vector_service = rag._get_vector_service()
        await vector_service.sync_config_from_db(db)
        compatible, vector_reason = vector_service.compatibility()
        result = await rag.hybrid_search(query=query, knowledge_items=candidates,
            filter_metadata=filters, top_k=effective_k, threshold=effective_threshold, strict_threshold=True,
            eligibility_date=today)
        configured = config.model_dump()
        trace = {
            "config_origin": origin,
            "config_fingerprint": hashlib.sha256(json.dumps(configured, sort_keys=True).encode()).hexdigest(),
            "configured_parameters": configured,
            "effective_parameters": {"top_k": effective_k, "similarity_threshold": effective_threshold,
                "use_rewrite": config.use_rewrite, "max_context_length": config.max_context_length},
            "filter_metadata": filters, "eligibility_date": today.isoformat(),
            "current_rows_in_scope": len(rows), "eligible_candidates": len(eligible),
            "excluded_by_dates": len(rows) - len(dated), "excluded_by_source": len(dated) - len(eligible), "context_chars": len(result.context_text),
            "retrieval_path": result.retrieval_path,
            "vector_index_compatible": compatible, "vector_index_reason": vector_reason,
            "embedding_contract": vector_service.contract,
            "index_generation": vector_service.get_index_info().get("generation"),
            "index_note": "配置身份不匹配时不使用稠密向量；本次未自动重建索引",
            "source_note": "来源和等级来自数据库标注，未自动核验其权威性或规范适用性",
        }
        return result, trace
