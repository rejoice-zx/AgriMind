# -*- coding: utf-8 -*-
"""
AgriMind 专家工作台与多模态图像诊断 API 路由
"""

import os
import uuid
import hashlib
from fastapi.responses import FileResponse
from app.models.diagnosis_media import DiagnosisMedia
from app.services.diagnosis_media_service import DiagnosisMediaService, NEW_PREFIX, OLD_PREFIX
from app.services.diagnosis_request_service import DiagnosisRequestService
import aiofiles
from typing import Optional, Literal
from sqlalchemy import select, func, insert, literal
from app.models import DiseaseRecord, Field, Farm
from app.services.farm_service import FarmService
from app.services.disease_review_service import DiseaseReviewService
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.api.dependencies import get_current_user, require_admin
from app.models.user import User
from app.schemas.expert import (
    ExpertResolveRequest,
    ExpertResolveResponse,
    ExpertTicketDetail,
    ExpertTicketItem,
    ExpertTicketListResponse,
    ImageUploadResponse,
    DiagnosisRequestStatus,
    VisualDiagnosisRequest,
    VisualDiagnosisResponse,
)
from app.services.expert_service import ExpertWorkbenchService
from app.core.roles import is_agri_staff
from app.core.analysis_lifecycle import run_owned_analysis

router = APIRouter(prefix="/api", tags=["农技专家工作台与多模态诊断"])


async def require_expert_or_staff(current_user: User = Depends(get_current_user)) -> User:
    """权限校验：仅农技专家或管理员；历史客服不具有专家权限"""
    if not is_agri_staff(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要农技专家或系统管理员权限",
        )
    return current_user


# ==================== 多模态图像上传与视觉初诊 ====================

@router.post("/diagnosis/upload", response_model=ImageUploadResponse, summary="上传病叶或植株现场图片")
async def upload_diagnosis_image(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    上传病叶照片，用于多模态视觉初诊或专家工单复核（必须登录）
    """
    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    filename = file.filename or "leaf.jpg"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的图片格式: {ext}，请上传 jpg、jpeg、png 或 webp 格式照片",
        )

    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="上传的文件为空，请提供有效的作物病斑照片",
        )
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="图片文件大小超出限制（最大允许 10MB）",
        )

    # 文件头与尺寸检查，不等于完整解码或植物内容真实性验证
    from app.services.vision_service import parse_image_dimensions
    fmt, width, height = parse_image_dimensions(content)
    if fmt == "unknown":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文件内容不是有效的图片格式，仅支持真实的 JPG、PNG 或 WEBP 格式图片",
        )
    if width < 200 or height < 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"图片分辨率过低 ({width}x{height})，有效病斑识别要求至少 200x200 像素，请上传清晰真实的田间叶片照片",
        )

    actor_id, actor_subject = current_user.id, current_user.auth_subject
    await DiagnosisMediaService.live_user(db, current_user)
    unique_filename = f"{uuid.uuid4().hex}.{fmt}"
    file_path = DiagnosisMediaService.path(unique_filename)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(file_path, "xb") as f:
        await f.write(content)
    mime = "image/jpeg" if fmt in {"jpeg", "jpg"} else f"image/{fmt}"
    values = {"id": unique_filename, "owner_id": actor_id, "content_type": mime,
        "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    eligible = select(User.id).where(User.id == actor_id, User.auth_subject == actor_subject,
        User.is_active.is_(True)).exists()
    try:
        saved = await db.scalar(insert(DiagnosisMedia).from_select(list(values),
            select(*[literal(value, type_=DiagnosisMedia.__table__.c[key].type) for key, value in values.items()]).where(eligible)).returning(DiagnosisMedia.id))
        if saved is None:
            raise HTTPException(409, "上传期间账号状态已变化，图片未关联，请重新登录")
        await db.commit()
    except Exception:
        await db.rollback()
        # A newly created orphan stays private; no destructive cleanup of shared files.
        raise
    return ImageUploadResponse(filename=unique_filename, url=NEW_PREFIX + unique_filename,
        size=len(content), content_type=mime)


@router.get("/diagnosis/media/{filename}")
async def read_diagnosis_media(filename: str, db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user)):
    path, mime = await DiagnosisMediaService.access(db, NEW_PREFIX + filename, current_user)
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, no-store",
        "Vary": "Authorization", "X-Content-Type-Options": "nosniff"})


# The /api prefix is not present on the old static path. It has a dedicated protected router.
legacy_media_router = APIRouter(tags=["旧病害图片授权访问"])
@legacy_media_router.get("/static/uploads/diagnosis/{filename:path}")
async def read_legacy_media(filename: str, db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user)):
    path, mime = await DiagnosisMediaService.access(db, OLD_PREFIX + filename, current_user)
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, no-store",
        "Vary": "Authorization", "X-Content-Type-Options": "nosniff"})



@router.post("/diagnosis/visual-diagnose", response_model=VisualDiagnosisResponse, summary="执行多模态图像初诊与工单自动分流")
async def execute_visual_diagnosis(
    request: VisualDiagnosisRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    作物多模态视觉初诊：
    - 输入病叶照片 URL、作物与症状提示
    - 识别典型病斑表型（灰霉病/早疫病/晚疫病/叶霉病等）
    - 模型评分有效且较高：返回初步建议和知识检索结果，仍需核实
    - 置信度 < 0.70：关联有权访问的田块且图片有效时创建专家复核工单
    - 未关联田块：仅返回诊断结果，不保存病害记录或创建工单
    """
    return await run_owned_analysis(db, request, current_user, 'diagnosis', DiagnosisRequestService.run)


@router.get("/diagnosis/requests/{request_id}", response_model=DiagnosisRequestStatus)
async def get_diagnosis_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user)):
    return await DiagnosisRequestService.status(db, request_id, current_user)


@router.post('/diagnosis/requests/{request_id}/cancel', response_model=DiagnosisRequestStatus)
async def cancel_diagnosis_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user)):
    return await DiagnosisRequestService.cancel(db, request_id, current_user)


# 所属农户复用诊断页查看反馈；权限以田块归属校验。
@router.get("/diagnosis/records", response_model=ExpertTicketListResponse)
async def list_owned_diagnoses(field_id: Optional[int] = Query(None, ge=1),
        offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
        db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    if field_id is not None:
        await FarmService.ensure_field_access(db, field_id, current_user)
    conditions = []
    if field_id is not None: conditions.append(DiseaseRecord.field_id == field_id)
    if not is_agri_staff(current_user): conditions.append(Farm.owner_id == current_user.id)
    query = DiseaseReviewService.record_query().join(Field, Field.id == DiseaseRecord.field_id).join(Farm, Farm.id == Field.farm_id).where(*conditions)
    total = await db.scalar(select(func.count()).select_from(DiseaseRecord).join(Field).join(Farm).where(*conditions))
    records = (await db.scalars(query.order_by(DiseaseRecord.created_at.desc(), DiseaseRecord.id.desc()).offset(offset).limit(limit))).all()
    return ExpertTicketListResponse(total=total or 0, items=[ExpertWorkbenchService.ticket_item(row) for row in records])


@router.get("/diagnosis/records/{record_id}", response_model=ExpertTicketDetail)
async def get_owned_diagnosis(record_id: int, db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user)):
    record = await DiseaseReviewService.record(db, record_id)
    await FarmService.ensure_field_access(db, record.field_id, current_user)
    return await ExpertWorkbenchService.get_ticket_detail(db, record_id)


@router.post("/diagnosis/records/{record_id}/request-review", response_model=ExpertTicketItem)
async def request_diagnosis_review(record_id: int, db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user)):
    record = await DiseaseReviewService.record(db, record_id)
    await FarmService.ensure_field_access(db, record.field_id, current_user)
    await DiseaseReviewService.request_review(db, record_id)
    return ExpertWorkbenchService.ticket_item(await DiseaseReviewService.record(db, record_id))


@router.delete("/diagnosis/records/{record_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="管理员删除病害记录及关联专家反馈")
async def delete_diagnosis_record(record_id: int, db: AsyncSession = Depends(get_db),
        current_user: User = Depends(require_admin())):
    await DiseaseReviewService.delete_record(db, record_id, current_user)


# ==================== 农技专家工作台工单流转 ====================

@router.get("/expert/tickets", response_model=ExpertTicketListResponse, summary="专家工作台待复核工单列表")
async def list_expert_tickets(
    status: Optional[Literal["pending_review", "in_review", "expert_reviewed"]] = Query(None, description="状态筛选：pending_review / in_review / expert_reviewed"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    limit: Optional[int] = Query(None, ge=1, le=100, description="兼容参数 limit"),
    offset: Optional[int] = Query(None, ge=0, description="兼容参数 offset"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_expert_or_staff),
):
    """
    获取专家工作台待办工单队列（支持状态筛选与分页）
    """
    effective_page_size = limit if limit is not None else page_size
    effective_page = page

    items, total = await ExpertWorkbenchService.list_tickets(
        db=db,
        status=status,
        page=effective_page,
        page_size=effective_page_size,
        offset=offset,
    )
    return ExpertTicketListResponse(total=total, items=items)


@router.get("/expert/tickets/{ticket_id}", response_model=ExpertTicketDetail, summary="获取工单全时空环境上下文详情")
async def get_expert_ticket_detail(
    ticket_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_expert_or_staff),
):
    """
    获取工单详情，聚合现场病斑照片、田块土壤、生长期、近期微气象与农事用药历史
    """
    detail = await ExpertWorkbenchService.get_ticket_detail(db, ticket_id)
    if not detail:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到工单 #{ticket_id}",
        )
    return detail


@router.post("/expert/tickets/{ticket_id}/claim", response_model=ExpertTicketItem, summary="专家认领待复核工单")
async def claim_expert_ticket(
    ticket_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_expert_or_staff),
):
    """
    专家认领工单：将状态由 pending_review 推进为 in_review
    """
    return await ExpertWorkbenchService.claim_ticket(
        db=db,
        ticket_id=ticket_id,
        expert_user=current_user,
    )


@router.post("/expert/tickets/{ticket_id}/resolve", response_model=ExpertResolveResponse, summary="专家提交最终确诊处方并沉淀知识")
async def resolve_expert_ticket(
    ticket_id: int,
    resolve_in: ExpertResolveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_expert_or_staff),
):
    """
    专家确诊并出具综合防治处方：
    - 完成工单闭环（状态置为 expert_reviewed）
    - 归档生成 ExpertCase 实体
    - 仅显式选择时保存 L4 专家案例；不代表标准认证，不自动重建索引
    """
    return await ExpertWorkbenchService.resolve_ticket(
        db=db,
        ticket_id=ticket_id,
        expert_user=current_user,
        resolve_in=resolve_in,
    )
