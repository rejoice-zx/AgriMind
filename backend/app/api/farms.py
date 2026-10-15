"""Farms and Fields API - 农场、地块与作物管理接口"""
from fastapi import APIRouter, Depends, Query, status
from app.services.location_service import regions, search_places
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.database import get_db
from app.api.dependencies import get_current_user
from app.models.user import User
from app.schemas.agriculture import (
    FarmCreate, FarmUpdate, FarmResponse, FarmListResponse,
    FieldCreate, FieldUpdate, FieldResponse, FieldListResponse,
    CropPlantingCreate, CropPlantingUpdate, CropPlantingResponse, CropPlantingListResponse, ArchiveRequest
)
from app.services.farm_service import FarmService
from app.core.roles import is_admin as roles_is_admin, is_agri_staff

router = APIRouter(prefix="/api", tags=["农业资产与种植管理"])

from app.schemas.field_environment import EnvironmentObservationCreate, FieldEnvironmentResponse, EnvironmentSubmissionResponse
from app.services.field_environment_service import FieldEnvironmentService
from app.schemas.field_environment import EnvironmentHistoryPage
from app.schemas.crop_history import CropHistoryPage, CropStatusEventPage, CropReopenRequest, CropReopenResponse
from app.services.crop_history_service import CropHistoryService


@router.get('/fields/{id}/environment-observations', response_model=EnvironmentHistoryPage, summary='按登记顺序分页查看原始环境观测')
async def environment_history(id: int, before_id: Optional[int] = Query(None, ge=1),
    snapshot_id: Optional[int] = Query(None, ge=0), limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await FieldEnvironmentService.history(db, id, current_user, before_id=before_id, snapshot_id=snapshot_id, limit=limit)


@router.get('/fields/{id}/crop-history', response_model=CropHistoryPage, summary='查看田块全部种植批次档案')
async def crop_history(id: int, before_id: Optional[int] = Query(None, ge=1),
    snapshot_id: Optional[int] = Query(None, ge=0), limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await CropHistoryService.list_crops(db, id, current_user, before_id=before_id, snapshot_id=snapshot_id, limit=limit)


@router.get('/crops/{id}/status-history', response_model=CropStatusEventPage, summary='批次结束与恢复的状态历史')
async def crop_status_history(id: int, before_id: Optional[int] = Query(None, ge=1),
    snapshot_id: Optional[int] = Query(None, ge=0), limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await CropHistoryService.list_events(db, id, current_user, before_id=before_id, snapshot_id=snapshot_id, limit=limit)


@router.post('/crops/{id}/reopen', response_model=CropReopenResponse, summary='所有者或管理员填写原因后恢复结束批次')
async def reopen_crop(id: int, payload: CropReopenRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await CropHistoryService.reopen(db, id, payload, current_user)


@router.get('/fields/{id}/environment', response_model=FieldEnvironmentResponse, summary='田块环境与最近人工观测')
async def field_environment(id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    field = await FarmService.ensure_field_access(db, id, current_user)
    return await FieldEnvironmentService.latest(db, field)


@router.post('/fields/{id}/environment-observations', response_model=EnvironmentSubmissionResponse, summary='追加现场观测，不覆盖历史')
async def record_environment(id: int, payload: EnvironmentObservationCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await FieldEnvironmentService.create(db, id, payload, current_user)


@router.post('/farms/{id}/archive', response_model=FarmResponse, summary='归档或恢复农场，保留所有历史')
async def archive_farm(id: int, payload: ArchiveRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await FarmService.set_archive(db, 'farm', id, payload.archived, current_user)


@router.post('/fields/{id}/archive', response_model=FieldResponse, summary='归档或恢复田块，保留所有历史')
async def archive_field(id: int, payload: ArchiveRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await FarmService.set_archive(db, 'field', id, payload.archived, current_user)


# ==================== 农场接口 (Farms) ====================

@router.get('/locations/regions')
async def list_regions(current_user: User = Depends(get_current_user)):
    return {'items': regions(), 'version': '2023-06-30', 'notice': '2023年区划数据；未收录地区可手动填写'}


@router.get('/locations/search')
async def lookup_location(name: str = Query(..., min_length=2, max_length=100),
                          region_code: Optional[str] = Query(None, pattern=r'^\d{6}$'),
                          province: Optional[str] = Query(None, max_length=50),
                          city: Optional[str] = Query(None, max_length=50),
                          current_user: User = Depends(get_current_user)):
    return await search_places(name, region_code=region_code, province=province, city=city)


@router.get('/farms/{id}/weather')
async def farm_weather(id: int, days: int = Query(7, ge=1, le=14),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    from types import SimpleNamespace
    from app.services.weather_service import WeatherService
    farm = await FarmService.ensure_farm_access(db, id, current_user)
    return await WeatherService.get_field_weather(db,
        SimpleNamespace(id=None, farm_id=farm.id, latitude=None, longitude=None), days)

@router.get("/farms", response_model=FarmListResponse, summary="获取农场列表")
async def list_farms(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    获取农场列表。
    - 普通用户/农户：获取自己名下的农场
    - 管理员/专家：可查看所有农场
    """
    is_admin = is_agri_staff(current_user)
    items, total = await FarmService.list_farms(
        db,
        owner_id=current_user.id,
        is_admin=is_admin,
        skip=skip,
        limit=limit
    )
    return FarmListResponse(total=total, items=items)


@router.post("/farms", response_model=FarmResponse, status_code=status.HTTP_201_CREATED, summary="创建新农场")
async def create_farm(
    farm_in: FarmCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """创建新农场，创建者自动成为农场拥有者"""
    return await FarmService.create_farm(db, farm_in, owner_id=current_user.id)


@router.get("/farms/{id}", response_model=FarmResponse, summary="获取农场详情")
async def get_farm(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取指定农场详细信息（含归属权限校验）"""
    return await FarmService.ensure_farm_access(db, id, current_user)


@router.put("/farms/{id}", response_model=FarmResponse, summary="更新农场信息")
async def update_farm(
    id: int,
    farm_in: FarmUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新农场信息（仅所有者或管理员可修改）"""
    is_admin = roles_is_admin(current_user)
    return await FarmService.update_farm(db, id, farm_in, user_id=current_user.id, is_admin=is_admin)


@router.delete("/farms/{id}", status_code=status.HTTP_200_OK, summary="删除农场")
async def delete_farm(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除农场（级联删除下属田块及种植记录）"""
    is_admin = roles_is_admin(current_user)
    await FarmService.delete_farm(db, id, user_id=current_user.id, is_admin=is_admin)
    return {"message": "农场删除成功", "id": id}


# ==================== 地块接口 (Fields) ====================

@router.get("/farms/{farm_id}/fields", response_model=FieldListResponse, summary="获取指定农场地块列表")
async def list_fields_by_farm(
    farm_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取农场下的所有田块/大棚/分区"""
    is_admin = is_agri_staff(current_user)
    fields = await FarmService.list_fields_by_farm(
        db,
        farm_id=farm_id,
        user_id=current_user.id,
        is_admin=is_admin
    )
    return FieldListResponse(total=len(fields), items=fields)


@router.post("/farms/{farm_id}/fields", response_model=FieldResponse, status_code=status.HTTP_201_CREATED, summary="在农场下创建田块")
async def create_field(
    farm_id: int,
    field_in: FieldCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """在指定农场下新增地块实体"""
    is_admin = roles_is_admin(current_user)
    return await FarmService.create_field(
        db,
        farm_id=farm_id,
        field_in=field_in,
        user_id=current_user.id,
        is_admin=is_admin
    )


@router.get("/fields/{id}", response_model=FieldResponse, summary="获取地块详情")
async def get_field(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """根据 ID 获取地块详细信息（含归属权限校验）"""
    return await FarmService.ensure_field_access(db, id, current_user)


@router.put("/fields/{id}", response_model=FieldResponse, summary="更新地块信息")
async def update_field(
    id: int,
    field_in: FieldUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新地块土壤、灌溉方式、面积等属性"""
    is_admin = roles_is_admin(current_user)
    return await FarmService.update_field(
        db,
        field_id=id,
        field_in=field_in,
        user_id=current_user.id,
        is_admin=is_admin
    )


@router.delete("/fields/{id}", status_code=status.HTTP_200_OK, summary="删除地块")
async def delete_field(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除地块（级联删除种植批次与时序数据）"""
    is_admin = roles_is_admin(current_user)
    await FarmService.delete_field(
        db,
        field_id=id,
        user_id=current_user.id,
        is_admin=is_admin
    )
    return {"message": "地块删除成功", "id": id}


# ==================== 作物批次接口 (CropPlantings) ====================

@router.get("/fields/{field_id}/crops", response_model=CropPlantingListResponse, summary="获取地块种植批次")
async def list_crops_by_field(
    field_id: int,
    status: Optional[str] = Query(None, description="active / harvested / terminated"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取指定地块的作物种植历史与当前种植档案（含地块权限校验）"""
    await FarmService.ensure_field_access(db, field_id, current_user)
    crops = await FarmService.list_crop_plantings_by_field(db, field_id, status_filter=status)
    return CropPlantingListResponse(total=len(crops), items=crops)


@router.post("/fields/{field_id}/crops", response_model=CropPlantingResponse, status_code=status.HTTP_201_CREATED, summary="添加作物种植档案")
async def create_crop_planting(
    field_id: int,
    crop_in: CropPlantingCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """为地块添加作物种植（如番茄、开花期、品种等）"""
    is_admin = roles_is_admin(current_user)
    return await FarmService.create_crop_planting(
        db,
        field_id=field_id,
        crop_in=crop_in,
        user_id=current_user.id,
        is_admin=is_admin
    )


@router.get("/crops/{id}", response_model=CropPlantingResponse, summary="获取种植档案详情")
async def get_crop_planting(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """根据 ID 获取作物种植档案详情（含归属权限校验）"""
    return await FarmService.ensure_crop_access(db, id, current_user)


@router.put("/crops/{id}", response_model=CropPlantingResponse, summary="更新种植批次或推进生长阶段")
async def update_crop_planting(
    id: int,
    crop_in: CropPlantingUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新作物档案（例如从苗期推进到开花期、坐果期）"""
    is_admin = roles_is_admin(current_user)
    return await FarmService.update_crop_planting(
        db,
        crop_id=id,
        crop_in=crop_in,
        user_id=current_user.id,
        is_admin=is_admin
    )


@router.delete("/crops/{id}", status_code=status.HTTP_200_OK, summary="删除作物种植批次")
async def delete_crop_planting(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除作物种植批次"""
    is_admin = roles_is_admin(current_user)
    await FarmService.delete_crop_planting(
        db,
        crop_id=id,
        user_id=current_user.id,
        is_admin=is_admin
    )
    return {"message": "作物种植档案删除成功", "id": id}
