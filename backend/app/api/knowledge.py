# -*- coding: utf-8 -*-
"""
知识库API端点 - Knowledge API

功能:
- 知识库CRUD操作
- 分页和筛选
- 导入导出
- 向量索引管理
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
import json

from app.database import get_db
from app.api.dependencies import AdminRequired
from app.models.user import User
from app.services.knowledge_service import knowledge_service
from app.schemas.knowledge import (
    CreateKnowledgeRequest,
    UpdateKnowledgeRequest,
    KnowledgeResponse,
    KnowledgeListResponse,
    ImportKnowledgeRequest,
    ImportKnowledgeResponse,
    ExportKnowledgeResponse,
    IndexStatusResponse,
    HybridSearchRequest,
    HybridSearchResponse,
    HybridSearchItem,
)
from app.services.agri_retrieval_service import AgriRetrievalService

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.post("/hybrid-search", response_model=HybridSearchResponse, summary="农业知识库 Hybrid RAG 检索诊断")
async def hybrid_search_knowledge(
    request: HybridSearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    农业 Hybrid RAG 检索诊断接口:
    - 实体自动识别 + Query Router 意图路由 (FACT, DIAGNOSIS, DECISION, PLANNING, GENERAL)
    - 结合 crop / growth_stage / topic / disease 进行元数据前置过滤
    - FAISS 向量稠密检索 + BM25 稀疏检索双路融合重排
    - 置信度计算与专家复核判定
    """
    filters = {key: value for key, value in dict(crop=request.crop, growth_stage=request.growth_stage,
        topic=request.topic, region=request.region).items() if value}
    search_result, retrieval_trace = await AgriRetrievalService.search(db, request.query, filters,
        top_k=request.top_k, threshold=request.min_confidence)

    return HybridSearchResponse(
        query=search_result.query,
        rewritten_query=search_result.rewritten_query,
        intent=search_result.intent,
        extracted_entities=search_result.extracted_entities,
        retrieved_items=[
            HybridSearchItem(
                id=item["id"],
                question=item["question"],
                answer=item["answer"],
                score=item["score"],
                vector_score=item.get("vector_score", 0.0),
                bm25_score=item.get("bm25_score", 0.0),
                authority_level=item.get("authority_level") or "未核验",
                source_name=item.get("source_name"),
                publish_date=item.get("publish_date"),
                url=item.get("url"),
                crop=item.get("crop"),
                growth_stage=item.get("growth_stage"),
                disease=item.get("disease"),
            )
            for item in search_result.retrieved_items
        ],
        context_text=search_result.context_text,
        confidence=search_result.confidence,
        need_expert=search_result.need_expert,
        retrieval_trace=retrieval_trace,
        expert_reason=search_result.expert_reason,
    )


@router.get("", response_model=KnowledgeListResponse)
async def get_knowledge_list(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    category: Optional[str] = Query(None, description="分类筛选"),
    keyword: Optional[str] = Query(None, description="关键词筛选"),
    crop: Optional[str] = Query(None, description="作物筛选（如番茄）"),
    growth_stage: Optional[str] = Query(None, description="生长阶段筛选"),
    topic: Optional[str] = Query(None, description="主题筛选"),
    source_type: Optional[str] = Query(None, description="来源类型筛选"),
    region: Optional[str] = Query(None, max_length=100, description="适用地域筛选"),
    disease: Optional[str] = Query(None, max_length=100, description="病害筛选"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    获取知识列表（分页、筛选）

    - 需要管理员权限
    - 支持按通用分类、作物、生长期、主题及关键词综合筛选
    """
    items, total = await knowledge_service.get_knowledge_list(
        db=db,
        page=page,
        page_size=page_size,
        category=category,
        keyword=keyword,
        crop=crop,
        growth_stage=growth_stage,
        topic=topic,
        source_type=source_type,
        region=region,
        disease=disease
    )

    return KnowledgeListResponse(
        items=[KnowledgeResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size
    )


@router.post("", response_model=KnowledgeResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge(
    request: CreateKnowledgeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    添加知识条目（支持农业领域元数据与版本管理）

    - 需要管理员权限
    - 自动检查重复
    - 自动同步向量索引
    """
    # 检查重复
    duplicate = await knowledge_service.check_duplicate(db, request.question)
    if duplicate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="知识条目已存在（问题重复）"
        )

    item = await knowledge_service.create_knowledge(
        db=db,
        question=request.question,
        answer=request.answer,
        keywords=request.keywords,
        category=request.category,
        score=request.score,
        crop=request.crop,
        region=request.region,
        growth_stage=request.growth_stage,
        topic=request.topic,
        disease=request.disease,
        source_type=request.source_type,
        source_name=request.source_name,
        authority_level=request.authority_level,
        version=request.version,
        publish_date=request.publish_date,
        url=request.url,
        source_updated_at=request.source_updated_at,
        effective_from=request.effective_from,
        effective_to=request.effective_to,
        is_current=request.is_current
    )

    return KnowledgeResponse.model_validate(item)


@router.get("/categories/list")
async def get_knowledge_categories(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """获取知识库所有分类"""
    cats = await knowledge_service.get_categories(db)
    return {"categories": cats}


@router.get("/sources", summary="获取知识库来源分级说明")
async def get_knowledge_sources(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """分类说明不是对实际条目、产品或规范的核验结果。"""
    return {
        "note": "来源分级是录入标注，不代表条目已核验。未核验/隔离来源不进入候选检索。",
        "sources": [
            {"level": "L1", "name": "权威规范", "description": "国家标准、农业行业技术规程、农药登记官方数据库", "priority": 1},
            {"level": "L2", "name": "专业文献", "description": "中国农科院/高校农业植保核心期刊与病虫害专著", "priority": 2},
            {"level": "L3", "name": "农技推广", "description": "省市级农技推广总站发布的栽培防病指南与田头测报", "priority": 3},
            {"level": "L4", "name": "专家案例", "description": "资深植保专家线上复核确诊的真实田间处置案例", "priority": 4},
        ]
    }


@router.get("/versions", summary="读取知识库版本标注与候选统计")
async def get_knowledge_versions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """Statistics describe stored metadata; they do not certify sources or crop support."""
    return await knowledge_service.lifecycle_summary(db)


@router.delete("/by-category/{category}", status_code=status.HTTP_200_OK)
async def delete_knowledge_by_category(
    category: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    删除指定分类下的所有知识条目

    - 需要管理员权限
    - 自动同步向量索引
    """
    count = await knowledge_service.delete_by_category(db, category)
    return {"deleted_count": count, "category": category}


@router.get("/{knowledge_id}", response_model=KnowledgeResponse)
async def get_knowledge(
    knowledge_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    获取知识条目详情

    - 需要管理员权限
    """
    item = await knowledge_service.get_knowledge(db, knowledge_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识条目不存在"
        )

    return KnowledgeResponse.model_validate(item)


@router.put("/{knowledge_id}", response_model=KnowledgeResponse)
async def update_knowledge(
    knowledge_id: str,
    request: UpdateKnowledgeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    更新知识条目

    - 需要管理员权限
    - 自动检查重复（如果更新问题）
    - 自动同步向量索引
    """
    # 检查是否存在
    existing = await knowledge_service.get_knowledge(db, knowledge_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识条目不存在"
        )

    # 如果更新问题，检查重复
    if request.question and request.question != existing.question:
        duplicate = await knowledge_service.check_duplicate(
            db, request.question, exclude_id=knowledge_id
        )
        if duplicate:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="知识条目已存在（问题重复）"
            )

    item = await knowledge_service.update_knowledge(
        db=db,
        knowledge_id=knowledge_id,
        question=request.question,
        answer=request.answer,
        keywords=request.keywords,
        category=request.category,
        score=request.score,
        crop=request.crop,
        region=request.region,
        growth_stage=request.growth_stage,
        topic=request.topic,
        disease=request.disease,
        source_type=request.source_type,
        source_name=request.source_name,
        authority_level=request.authority_level,
        version=request.version,
        publish_date=request.publish_date,
        url=request.url,
        source_updated_at=request.source_updated_at,
        is_current=request.is_current,
        effective_from=(request.effective_from or "") if "effective_from" in request.model_fields_set else None,
        effective_to=(request.effective_to or "") if "effective_to" in request.model_fields_set else None
    )

    return KnowledgeResponse.model_validate(item)


@router.delete("/{knowledge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge(
    knowledge_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    删除知识条目

    - 需要管理员权限
    - 自动同步向量索引
    """
    success = await knowledge_service.delete_knowledge(db, knowledge_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识条目不存在"
        )

    return None


# ==================== 导入导出 ====================


@router.post("/import", response_model=ImportKnowledgeResponse)
async def import_knowledge(
    request: ImportKnowledgeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    批量导入知识条目

    - 需要管理员权限
    - 支持跳过重复项
    """
    success_count, skip_count, errors = await knowledge_service.import_items(
        db=db,
        items=[item.model_dump() for item in request.items],
        skip_duplicates=request.skip_duplicates
    )

    return ImportKnowledgeResponse(
        success_count=success_count,
        skip_count=skip_count,
        errors=errors
    )


@router.get("/export/all", response_model=ExportKnowledgeResponse)
async def export_knowledge(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    导出所有知识条目

    - 需要管理员权限
    """
    items = await knowledge_service.export_all(db)

    return ExportKnowledgeResponse(
        items=items,
        total=len(items)
    )


# ==================== 向量索引管理 ====================

@router.get("/index/status", response_model=IndexStatusResponse)
async def get_index_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    获取向量索引状态

    - 需要管理员权限
    - 从数据库读取Embedding配置状态
    """
    from app.services.vector_service import VectorService
    vector = VectorService()
    ready = await vector.sync_config_from_db(db)
    info = vector.get_index_info()
    compatible, reason = vector.compatibility()
    damaged, damage_reason = vector.needs_rebuild()
    needs_rebuild = damaged or (bool(info["count"]) and not compatible)
    if damaged:
        reason = damage_reason
    return IndexStatusResponse(count=info["count"], dimension=info["dimension"],
        embedding_model=info["embedding_model"], embedding_available=ready,
        embedding_dimension=vector.embedding_dimension if ready else None,
        needs_rebuild=needs_rebuild, rebuild_reason=reason if needs_rebuild else None,
        index_generation=info.get("generation"), index_contract=info.get("embedding_contract"),
        current_contract=vector.contract, vector_compatible=compatible)


@router.post("/index/rebuild")
async def rebuild_index(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    重建向量索引（SSE 流式进度）

    - 需要管理员权限
    - 会先从数据库同步Embedding配置
    - 然后清空现有索引并重新构建
    - 返回 SSE 事件流
    """
    from sqlalchemy import select
    from app.models.knowledge import KnowledgeItem as KBModel

    from app.services.knowledge_service import KnowledgeService, _chunk_context
    worker = KnowledgeService()
    # Bind before collection, preserving the published old generation until a complete candidate is ready.
    config_ok = await worker.vector_service.sync_config_from_db(db)
    if not config_ok:
        # 直接返回错误 SSE
        async def _err():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Embedding服务不可用：未配置API密钥'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(_err(), media_type="text/event-stream")

    # 从数据库同步 chunk 配置到运行时 Config
    from app.services.settings_service import SettingsService
    from app.core.config import Config as RuntimeConfig
    rag_cfg = await SettingsService.get_rag_config(db)
    RuntimeConfig().apply_rag_config(rag_cfg)

    rebuild_context = worker.vector_service.begin_rebuild()
    result = await db.execute(select(KBModel))
    items = list(result.scalars().all())
    total = len(items)

    async def build_stream():
        success_count = 0
        fail_count = 0
        collected = []

        yield f"data: {json.dumps({'type': 'start', 'total': total}, ensure_ascii=False)}\n\n"

        for idx, item in enumerate(items):
            current = idx + 1
            try:
                chunks = worker._build_chunks(item)
                embeddings = await worker.vector_service.embed_texts(
                    [text for _, text in chunks]
                )
                collected.extend(worker._complete_chunk_vectors(chunks, embeddings))
                success_count += 1
            except Exception:
                fail_count += 1

            yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': total}, ensure_ascii=False)}\n\n"

        # 先完整构建再一次性原子发布：重建期间读者仍看到完整旧索引
        if fail_count:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Incomplete embeddings; previous generation retained', 'success_count': 0, 'fail_count': fail_count, 'total': total}, ensure_ascii=False)}\n\n"
            return
        if not await worker.vector_service.replace_all(collected, **rebuild_context):
            yield f"data: {json.dumps({'type': 'error', 'message': 'Candidate publication failed or concurrent change; previous generation retained', 'success_count': 0, 'total': total}, ensure_ascii=False)}\n\n"
            return

        yield f"data: {json.dumps({'type': 'done', 'success_count': success_count, 'fail_count': fail_count, 'total': total}, ensure_ascii=False)}\n\n"

    async def event_stream():
        contract = worker.vector_service.contract
        token = _chunk_context.set((contract["chunk_size"], contract["chunk_overlap"], contract["chunk_max_per_item"]))
        try:
            async for event in build_stream(): yield event
        finally: _chunk_context.reset(token)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
