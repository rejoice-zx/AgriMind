"""Farming Records & Disease Diagnosis API - 农事记录、病虫害与环境数据接口"""
from fastapi import APIRouter, Depends, Query, status, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Literal
from sqlalchemy import select
import re
from datetime import date

from app.database import get_db
from app.api.dependencies import get_current_user
from app.models.user import User
from app.schemas.agriculture import (
    FarmingRecordCreate, FarmingRecordCreateRequest, FarmingRecordResponse, FarmingRecordListResponse,
    DiseaseRecordCreate, DiseaseRecordResponse, DiseaseRecordListResponse,
    WeatherSnapshotCreate, WeatherSnapshotResponse, WeatherSnapshotListResponse,
    ExpertCaseCreate, ExpertCaseResponse
)
from app.services.farming_record_service import FarmingRecordService, deletion_state
from app.services.farm_service import FarmService
from app.core.roles import is_admin as roles_is_admin, is_agri_staff

router = APIRouter(prefix="/api", tags=["农事记录与病害诊断"])


async def revocation_names(db, items):
    subjects = {item.revoked_by_subject for item in items if item.revoked_by_subject}
    if not subjects:
        return {}
    rows = await db.execute(select(User.auth_subject, User.display_name, User.username).where(User.auth_subject.in_(subjects)))
    return {subject: display_name or username for subject, display_name, username in rows}


from app.schemas.weather import FieldWeatherResponse
from app.services.weather_service import WeatherService


@router.get("/fields/{field_id}/weather/forecast", response_model=FieldWeatherResponse)
async def get_field_weather_forecast(field_id: int, days: int = Query(7, ge=1, le=14),
        current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    field = await FarmService.ensure_field_access(db, field_id, current_user)
    return await WeatherService.get_field_weather(db, field, days)


# ==================== 农事记录接口 ====================

@router.get("/fields/{field_id}/farming-records", response_model=FarmingRecordListResponse, summary="获取地块农事记录")
async def list_farming_records(
    field_id: int,
    record_type: Optional[str] = Query(None, description="农事类型过滤：施肥/灌溉/打药等"),
    start_date: Optional[date] = Query(None, description="起始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    record_status: Literal['active', 'revoked', 'all'] = Query('active'),
    crop_planting_id: Optional[int] = Query(None, ge=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取指定地块的农事操作记录列表（含地块权限校验）"""
    await FarmService.ensure_field_access(db, field_id, current_user)
    if crop_planting_id is not None:
        crop = await FarmService.ensure_crop_access(db, crop_planting_id, current_user)
        if crop.field_id != field_id:
            raise HTTPException(422, '种植批次不属于当前田块')
    items, total = await FarmingRecordService.list_farming_records(
        db,
        field_id=field_id,
        record_type=record_type,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit,
        record_status=record_status,
        crop_planting_id=crop_planting_id
    )
    names = await revocation_names(db, items)
    return FarmingRecordListResponse(total=total, items=[FarmingRecordResponse.model_validate(item).model_copy(
        update={**deletion_state(item, current_user), 'revoked_by_name': names.get(item.revoked_by_subject)}) for item in items])


@router.post("/fields/{field_id}/farming-records", response_model=FarmingRecordResponse, status_code=status.HTTP_201_CREATED, summary="新增农事操作记录")
async def create_farming_record(
    field_id: int,
    record_in: FarmingRecordCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """新增农事操作记录（施肥、灌溉、修剪、打药等）"""
    is_admin = roles_is_admin(current_user)
    return await FarmingRecordService.create_farming_record(
        db,
        field_id=field_id,
        record_in=record_in,
        user_id=current_user.id,
        is_admin=is_admin
    )


@router.get("/farming-records/field/{field_id}", summary="获取地块农事记录（真实安全间隔期，杜绝假数据）")
async def list_farming_records_compat(
    field_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    record_status: Literal['active', 'revoked', 'all'] = Query('active'),
    crop_planting_id: Optional[int] = Query(None, ge=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """读取实际登记的间隔值及绑定批次，不自动认定标签合规。"""
    await FarmService.ensure_field_access(db, field_id, current_user)
    if crop_planting_id is not None:
        crop = await FarmService.ensure_crop_access(db, crop_planting_id, current_user)
        if crop.field_id != field_id:
            raise HTTPException(422, '种植批次不属于当前田块')
    items, total = await FarmingRecordService.list_farming_records(
        db,
        field_id=field_id,
        skip=skip,
        limit=limit,
        record_status=record_status,
        crop_planting_id=crop_planting_id
    )
    names = await revocation_names(db, items)
    formatted_items = []
    for item in items:
        # 严格获取真实安全间隔期，未查明时为 None，绝不默认假定 7 天
        real_phi = getattr(item, "safety_interval_days", None)

        # 兼容旧 attachments 数据
        if real_phi is None and isinstance(item.attachments, list) and len(item.attachments) > 0 and isinstance(item.attachments[0], dict):
            real_phi = item.attachments[0].get("safety_interval_days")
        elif real_phi is None and isinstance(item.attachments, dict):
            real_phi = item.attachments.get("safety_interval_days")

        formatted_items.append({
            "id": item.id,
            "field_id": item.field_id,
            "crop_planting_id": item.crop_planting_id,
            "record_type": item.record_type,
            "operation_date": str(item.record_date),
            "record_date": str(item.record_date),
            "operation_content": item.description or "",
            "description": item.description or "",
            "chemical_name": item.input_name or "",
            "input_name": item.input_name or "",
            "dosage": f"{item.input_amount or ''}{item.unit or ''}".strip() if (item.input_amount or item.unit) else (item.unit or ""),
            "input_amount": item.input_amount,
            "unit": item.unit,
            "operator": item.operator or "",
            "safety_interval_days": real_phi,  # 真实值或 None
            "created_at": str(item.created_at) if item.created_at else None,
            'revoked_at': item.revoked_at.isoformat() if item.revoked_at else None,
            'revoke_reason': item.revoke_reason,
            'revoked_by_name': names.get(item.revoked_by_subject),
            **deletion_state(item, current_user),
        })
    return formatted_items


@router.post("/farming-records", status_code=status.HTTP_201_CREATED, summary="新增农事操作记录")
async def create_farming_record_compat(
    payload: FarmingRecordCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """保留实际农事登记，未知安全间隔期留空，不自动套用历史候选。"""
    field_id = payload.field_id
    await FarmService.ensure_field_access(db, field_id, current_user)

    rec_date = payload.record_date
    if not rec_date:
        from app.core.weather_policy import agriculture_today
        rec_date = payload.operation_date or agriculture_today()

    desc = payload.operation_content or payload.description or "常规田间农事作业"
    chem = payload.chemical_name or payload.input_name
    dosage = payload.dosage
    input_amount = payload.input_amount
    unit = payload.unit
    safety_days = payload.safety_interval_days

    # 若传入 dosage 复合字符串（如 "100ml"、"50克"、"2.5kg"、"200"），结构化解析为 input_amount 与 unit
    if dosage and (input_amount is None or not unit):
        m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z\u4e00-\u9fa5/]*)\s*$", str(dosage).strip())
        if m:
            num_part, unit_part = m.groups()
            if input_amount is None:
                try:
                    input_amount = float(num_part)
                except Exception:
                    input_amount = None
            if not unit and unit_part:
                unit = unit_part.strip()
        elif not unit:
            unit = str(dosage).strip()

    if input_amount is not None:
        try:
            input_amount = float(input_amount)
        except Exception:
            input_amount = None

    # 保存用户记录的事实；本地候选不等同于已核验的产品标签。

    record_in = FarmingRecordCreate(
        request_id=payload.request_id,
        record_type=payload.record_type or "other",
        record_date=rec_date,
        crop_planting_id=payload.crop_planting_id,
        operator=payload.operator or None,
        description=desc,
        input_name=chem,
        input_amount=input_amount,
        unit=unit,
        safety_interval_days=safety_days,
        attachments=payload.attachments or [],
    )
    is_admin = roles_is_admin(current_user)
    record = await FarmingRecordService.create_farming_record(
        db,
        field_id=field_id,
        record_in=record_in,
        user_id=current_user.id,
        is_admin=is_admin
    )
    return {
        "id": record.id,
        "request_id": record.request_id,
        "replayed": bool(getattr(record, 'replayed', False)),
        "field_id": record.field_id,
        "crop_planting_id": record.crop_planting_id,
        "record_type": record.record_type,
        "operation_date": str(record.record_date),
        "record_date": str(record.record_date),
        "operation_content": record.description,
        "description": record.description,
        "chemical_name": record.input_name,
        "input_amount": record.input_amount,
        "unit": record.unit,
        "dosage": f"{record.input_amount or ''}{record.unit or ''}".strip() if (record.input_amount or record.unit) else (record.unit or ""),
        "operator": record.operator,
        "safety_interval_days": record.safety_interval_days,
    }



@router.delete("/farming-records/{id}", status_code=status.HTTP_200_OK, summary="删除农事记录")
async def delete_farming_record(
    id: int,
    reason: str = Query('误录撤销', min_length=1, max_length=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除农事记录"""
    is_admin = roles_is_admin(current_user)
    await FarmingRecordService.delete_farming_record(
        db,
        record_id=id,
        user_id=current_user.id,
        is_admin=is_admin,
        reason=reason
    )
    return {"message": "农事记录删除成功", "id": id}


# ==================== 病虫害记录接口 ====================

@router.get("/fields/{field_id}/disease-records", response_model=DiseaseRecordListResponse, summary="获取地块病害记录")
async def list_disease_records(
    field_id: int,
    status_filter: Optional[str] = Query(None, description="preliminary / expert_reviewed / resolved"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取指定地块历史病虫害排查与初诊记录（含地块权限校验）"""
    await FarmService.ensure_field_access(db, field_id, current_user)
    items = await FarmingRecordService.list_disease_records(
        db,
        field_id=field_id,
        status_filter=status_filter
    )
    return DiseaseRecordListResponse(total=len(items), items=items)


@router.post("/fields/{field_id}/disease-records", response_model=DiseaseRecordResponse, status_code=status.HTTP_201_CREATED, summary="提交病害初诊/巡查记录")
async def create_disease_record(
    field_id: int,
    disease_in: DiseaseRecordCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """提交人工巡查记录；AI结果与专家反馈通过专用流程写入"""
    await FarmService.ensure_field_owner_writable(db, field_id, current_user)
    if disease_in.image_url:
        from app.services.diagnosis_media_service import DiagnosisMediaService
        await DiagnosisMediaService.access(db, disease_in.image_url, current_user, for_link=True, field_id=field_id)
    is_admin = roles_is_admin(current_user)
    return await FarmingRecordService.create_disease_record(
        db,
        field_id=field_id,
        disease_in=disease_in,
        user_id=current_user.id,
        is_admin=is_admin
    )


@router.get("/disease-records/{id}", response_model=DiseaseRecordResponse, summary="获取病害记录详情")
async def get_disease_record(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取单条病害诊断记录详情（含地块归属校验）"""
    record = await FarmingRecordService.get_disease_record_by_id(db, id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="病害记录不存在")
    await FarmService.ensure_field_access(db, record.field_id, current_user)
    return record


# ==================== 微气象与时序环境数据接口 ====================

@router.post("/fields/{field_id}/weather", response_model=WeatherSnapshotResponse, status_code=status.HTTP_201_CREATED, summary="上传/记录微气象快照")
async def record_weather_snapshot(
    field_id: int,
    weather_in: WeatherSnapshotCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """记录田块气象快照（含地块权限校验）"""
    await FarmService.ensure_field_owner_writable(db, field_id, current_user)
    return await FarmingRecordService.record_weather_snapshot(db, field_id, weather_in)


@router.get("/fields/{field_id}/weather/latest", response_model=Optional[WeatherSnapshotResponse], summary="获取田块最新气象")
async def get_latest_weather(
    field_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取指定地块最新一条环境气象数据（含地块权限校验）"""
    await FarmService.ensure_field_access(db, field_id, current_user)
    return await FarmingRecordService.get_latest_weather(db, field_id)


@router.get("/fields/{field_id}/weather/history", response_model=WeatherSnapshotListResponse, summary="获取田块历史气象时序")
async def list_weather_history(
    field_id: int,
    limit: int = Query(24, ge=1, le=168, description="时序点数量（如最近24小时）"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取田块历史环境时序（含地块权限校验）"""
    await FarmService.ensure_field_access(db, field_id, current_user)
    items = await FarmingRecordService.list_weather_history(db, field_id, limit=limit)
    return WeatherSnapshotListResponse(total=len(items), items=items)


# ==================== 专家复核与案例归档接口 ====================

@router.post("/expert/cases", response_model=ExpertCaseResponse, status_code=status.HTTP_201_CREATED, summary="专家确诊并归档案例")
async def create_expert_case(
    case_in: ExpertCaseCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """农技专家复核低置信度初诊，给出确诊处方与农事管理建议，并沉淀为案例库候选条目"""
    if not is_agri_staff(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅农技专家或管理员可复核案例")
    return await FarmingRecordService.create_expert_case(db, case_in, expert_id=current_user.id)
