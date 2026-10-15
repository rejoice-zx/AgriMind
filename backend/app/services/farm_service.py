"""Farm Service - 农场、田块与作物档案业务逻辑服务"""
import logging
from typing import Optional, List, Tuple
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.models.farm import Farm
from app.models.field import Field
from app.models.crop import CropPlanting
from app.schemas.agriculture import (
    FarmCreate, FarmUpdate,
    FieldCreate, FieldUpdate,
    CropPlantingCreate, CropPlantingUpdate
)

logger = logging.getLogger(__name__)


class FarmService:
    """农场与田块管理服务"""

    @staticmethod
    def assert_writable(field=None, farm=None):
        if getattr(field, 'archived_at', None) is not None or getattr(farm, 'archived_at', None) is not None:
            raise HTTPException(409, '该农场或田块已归档，只能查看历史；恢复后才能新增或修改业务记录')

    @staticmethod
    async def ensure_field_writable(db, field_id, user):
        field = await FarmService.ensure_field_access(db, field_id, user)
        farm = await FarmService.get_farm_by_id(db, field.farm_id)
        FarmService.assert_writable(field, farm)
        return field

    @staticmethod
    async def set_archive(db, asset_type, asset_id, archived, user):
        asset = (await FarmService.ensure_farm_access(db, asset_id, user) if asset_type == 'farm'
                 else await FarmService.ensure_field_access(db, asset_id, user))
        farm = asset if asset_type == 'farm' else await FarmService.get_farm_by_id(db, asset.farm_id)
        if user.role != 'admin' and farm.owner_id != user.id:
            raise HTTPException(403, '只有所有者或管理员可以归档或恢复')
        if asset_type == 'field' and farm.archived_at and not archived:
            raise HTTPException(409, '请先恢复所属农场，再恢复田块')
        if bool(asset.archived_at) != archived:
            from app.utils.time import utcnow
            asset.archived_at = utcnow() if archived else None
            await db.commit()
            await db.refresh(asset)
            from app.core.audit_logging import audit
            audit('agricultural_asset_archive_changed', asset_type=asset_type, asset_id=asset_id,
                  actor_id=user.id, archive_status='archived' if archived else 'active')
        return asset

    # ==================== Farm 业务 ====================

    @staticmethod
    async def create_farm(db: AsyncSession, farm_in: FarmCreate, owner_id: int) -> Farm:
        """创建农场"""
        from app.services.location_service import validate_region
        validate_region(farm_in.region_code, farm_in.province, farm_in.city, farm_in.district)
        farm = Farm(
            owner_id=owner_id,
            name=farm_in.name,
            description=farm_in.description,
            province=farm_in.province,
            city=farm_in.city,
            district=farm_in.district,
            address=farm_in.address,
            region_code=farm_in.region_code,
            location_source=farm_in.location_source or ('manual' if farm_in.latitude is not None else None),
            location_label=farm_in.location_label,
            latitude=farm_in.latitude,
            longitude=farm_in.longitude,
            area=farm_in.area,
        )
        db.add(farm)
        await db.commit()
        await db.refresh(farm)
        return farm

    @staticmethod
    async def get_farm_by_id(db: AsyncSession, farm_id: int) -> Optional[Farm]:
        """根据 ID 查询农场"""
        stmt = select(Farm).where(Farm.id == farm_id)
        result = await db.execute(stmt)
        return result.scalars().first()

    @staticmethod
    async def ensure_farm_access(db: AsyncSession, farm_id: int, user) -> Farm:
        """校验农场归属权限，若无权访问抛出 403，不存在抛出 404"""
        farm = await FarmService.get_farm_by_id(db, farm_id)
        if not farm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="农场不存在")
        if getattr(user, "role", "") not in ("admin", "expert") and farm.owner_id != getattr(user, "id", None):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该农场数据")
        return farm

    @staticmethod
    async def ensure_field_access(db: AsyncSession, field_id: int, user) -> Field:
        """校验地块归属权限，若无权访问抛出 403，不存在抛出 404"""
        field = await FarmService.get_field_by_id(db, field_id)
        if not field:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="地块不存在")
        if getattr(user, "role", "") in ("admin", "expert"):
            return field
        farm = await FarmService.get_farm_by_id(db, field.farm_id)
        if not farm or farm.owner_id != getattr(user, "id", None):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该地块数据")
        return field

    @staticmethod
    async def ensure_crop_access(db: AsyncSession, crop_id: int, user) -> CropPlanting:
        """校验作物批次归属权限，若无权访问抛出 403，不存在抛出 404"""
        crop = await FarmService.get_crop_planting_by_id(db, crop_id)
        if not crop:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="作物种植档案不存在")
        if getattr(user, "role", "") in ("admin", "expert"):
            return crop
        field = await FarmService.get_field_by_id(db, crop.field_id)
        if not field:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="地块不存在")
        farm = await FarmService.get_farm_by_id(db, field.farm_id)
        if not farm or farm.owner_id != getattr(user, "id", None):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该作物档案")
        return crop

    @staticmethod
    async def list_farms(
        db: AsyncSession,
        owner_id: Optional[int] = None,
        is_admin: bool = False,
        skip: int = 0,
        limit: int = 100
    ) -> Tuple[List[Farm], int]:
        """列表查询农场"""
        conditions = []
        if not is_admin and owner_id is not None:
            conditions.append(Farm.owner_id == owner_id)

        count_stmt = select(func.count(Farm.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total_result = await db.execute(count_stmt)
        total = total_result.scalar_one()

        stmt = select(Farm)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(Farm.created_at.desc()).offset(skip).limit(limit)
        result = await db.execute(stmt)
        items = list(result.scalars().all())

        return items, total

    @staticmethod
    async def update_farm(
        db: AsyncSession,
        farm_id: int,
        farm_in: FarmUpdate,
        user_id: int,
        is_admin: bool = False
    ) -> Farm:
        """更新农场"""
        farm = await FarmService.get_farm_by_id(db, farm_id)
        if not farm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="农场不存在")
        if not is_admin and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权修改该农场")

        FarmService.assert_writable(farm=farm)
        update_data = farm_in.model_dump(exclude_unset=True)
        from app.services.location_service import validate_region
        merged = {key: update_data.get(key, getattr(farm, key)) for key in ('region_code', 'province', 'city', 'district')}
        validate_region(merged['region_code'], merged['province'], merged['city'], merged['district'])
        if any(key in update_data and update_data[key] != getattr(farm, key) for key in ('province', 'city', 'district', 'address')) and 'latitude' not in update_data:
            update_data.update(latitude=None, longitude=None, location_source=None, location_label=None)
        if 'latitude' in update_data and 'location_source' not in update_data:
            update_data.update(location_source='manual' if update_data['latitude'] is not None else None, location_label=None)
        for field, value in update_data.items():
            setattr(farm, field, value)

        await db.commit()
        await db.refresh(farm)
        return farm

    @staticmethod
    async def delete_farm(
        db: AsyncSession,
        farm_id: int,
        user_id: int,
        is_admin: bool = False
    ) -> bool:
        """删除农场"""
        farm = await FarmService.get_farm_by_id(db, farm_id)
        if not farm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="农场不存在")
        if not is_admin and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权删除该农场")

        raise HTTPException(409, "为保护追溯历史，农场物理删除已停用；请保留档案")

    # ==================== Field 业务 ====================

    @staticmethod
    async def create_field(
        db: AsyncSession,
        farm_id: int,
        field_in: FieldCreate,
        user_id: int,
        is_admin: bool = False
    ) -> Field:
        """在农场下创建田块"""
        farm = await FarmService.get_farm_by_id(db, farm_id)
        if not farm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="农场不存在")
        if not is_admin and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权在该农场添加地块")
        FarmService.assert_writable(farm=farm)

        field = Field(
            farm_id=farm_id,
            name=field_in.name,
            area=field_in.area,
            soil_type=field_in.soil_type,
            irrigation_type=field_in.irrigation_type,
            facility_type=field_in.facility_type,
            latitude=field_in.latitude,
            longitude=field_in.longitude,
            remark=field_in.remark,
        )
        db.add(field)
        await db.commit()
        await db.refresh(field)
        return field

    @staticmethod
    async def get_field_by_id(db: AsyncSession, field_id: int) -> Optional[Field]:
        """根据 ID 查询田块"""
        stmt = select(Field).where(Field.id == field_id)
        result = await db.execute(stmt)
        return result.scalars().first()

    @staticmethod
    async def list_fields_by_farm(
        db: AsyncSession,
        farm_id: int,
        user_id: Optional[int] = None,
        is_admin: bool = False
    ) -> List[Field]:
        """查询指定农场的所有地块"""
        farm = await FarmService.get_farm_by_id(db, farm_id)
        if not farm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="农场不存在")
        if not is_admin and user_id is not None and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该农场地块")

        stmt = select(Field).where(Field.farm_id == farm_id).order_by(Field.id.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def update_field(
        db: AsyncSession,
        field_id: int,
        field_in: FieldUpdate,
        user_id: int,
        is_admin: bool = False
    ) -> Field:
        """更新地块信息"""
        field = await FarmService.get_field_by_id(db, field_id)
        if not field:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="地块不存在")

        farm = await FarmService.get_farm_by_id(db, field.farm_id)
        if not is_admin and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权修改该地块")

        FarmService.assert_writable(field, farm)
        update_data = field_in.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(field, key, value)

        await db.commit()
        await db.refresh(field)
        return field

    @staticmethod
    async def delete_field(
        db: AsyncSession,
        field_id: int,
        user_id: int,
        is_admin: bool = False
    ) -> bool:
        """删除地块"""
        field = await FarmService.get_field_by_id(db, field_id)
        if not field:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="地块不存在")

        farm = await FarmService.get_farm_by_id(db, field.farm_id)
        if not is_admin and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权删除该地块")

        raise HTTPException(409, "为保护追溯历史，田块物理删除已停用；可结束所属种植批次")

    # ==================== CropPlanting 业务 ====================

    @staticmethod
    async def create_crop_planting(
        db: AsyncSession,
        field_id: int,
        crop_in: CropPlantingCreate,
        user_id: int,
        is_admin: bool = False
    ) -> CropPlanting:
        """在地块上建立作物种植批次"""
        field = await FarmService.get_field_by_id(db, field_id)
        if not field:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="地块不存在")

        farm = await FarmService.get_farm_by_id(db, field.farm_id)
        if not is_admin and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权为该地块添加作物种植")
        FarmService.assert_writable(field, farm)

        crop = CropPlanting(
            field_id=field_id,
            crop=crop_in.crop,
            variety=crop_in.variety,
            planting_date=crop_in.planting_date,
            expected_harvest_date=crop_in.expected_harvest_date,
            growth_stage=crop_in.growth_stage,
            area=crop_in.area if crop_in.area > 0 else field.area,
            status=crop_in.status,
        )
        db.add(crop)
        await db.commit()
        await db.refresh(crop)
        return crop

    @staticmethod
    async def get_crop_planting_by_id(db: AsyncSession, crop_id: int) -> Optional[CropPlanting]:
        """根据 ID 获取作物种植档案"""
        stmt = select(CropPlanting).where(CropPlanting.id == crop_id)
        result = await db.execute(stmt)
        return result.scalars().first()

    @staticmethod
    async def list_crop_plantings_by_field(
        db: AsyncSession,
        field_id: int,
        status_filter: Optional[str] = None
    ) -> List[CropPlanting]:
        """获取地块下的所有种植记录"""
        stmt = select(CropPlanting).where(CropPlanting.field_id == field_id)
        if status_filter:
            stmt = stmt.where(CropPlanting.status == status_filter)
        stmt = stmt.order_by(CropPlanting.planting_date.desc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def update_crop_planting(
        db: AsyncSession,
        crop_id: int,
        crop_in: CropPlantingUpdate,
        user_id: int,
        is_admin: bool = False
    ) -> CropPlanting:
        """更新作物种植信息或生长阶段"""
        crop = await FarmService.get_crop_planting_by_id(db, crop_id)
        if not crop:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="作物种植记录不存在")

        field = await FarmService.get_field_by_id(db, crop.field_id)
        farm = await FarmService.get_farm_by_id(db, field.farm_id)
        if not is_admin and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权修改该作物种植记录")

        FarmService.assert_writable(field, farm)
        update_data = crop_in.model_dump(exclude_unset=True)
        planting_date = update_data.get('planting_date', crop.planting_date)
        harvest_date = update_data.get('expected_harvest_date', crop.expected_harvest_date)
        if planting_date is None or (harvest_date and harvest_date < planting_date):
            raise HTTPException(422, '预计采收日期不能早于种植日期')
        if any(key in update_data and update_data[key] is None for key in ('crop', 'growth_stage', 'area', 'status')):
            raise HTTPException(422, '作物、阶段、面积和状态不能为空')
        if crop.status != 'active' and update_data.get('status') == 'active':
            raise HTTPException(409, '结束批次必须通过填写原因的恢复入口重新启用，普通编辑不能恢复')
        if 'status' in update_data and update_data['status'] != crop.status:
            from app.models import User, CropStatusEvent
            from app.utils.time import utcnow
            from sqlalchemy import update
            actor = await db.get(User, user_id)
            if actor is None or not actor.is_active:
                raise HTTPException(401, '账号状态已变化，请重新登录')
            event = CropStatusEvent(crop_planting_id=crop.id, previous_status=crop.status,
                new_status=update_data['status'], created_by_subject=actor.auth_subject,
                actor_name=actor.display_name or actor.username)
            changed = await db.execute(update(CropPlanting).where(CropPlanting.id == crop.id,
                CropPlanting.status == crop.status).values(**update_data, updated_at=utcnow())
                .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                await db.rollback()
                raise HTTPException(409, '批次状态已变化，请刷新后重新操作')
            db.add(event)
            await db.commit()
            await db.refresh(crop)
            return crop
        for key, value in update_data.items():
            setattr(crop, key, value)

        await db.commit()
        await db.refresh(crop)
        return crop

    @staticmethod
    async def delete_crop_planting(
        db: AsyncSession,
        crop_id: int,
        user_id: int,
        is_admin: bool = False
    ) -> bool:
        """删除作物种植批次"""
        crop = await FarmService.get_crop_planting_by_id(db, crop_id)
        if not crop:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="作物种植记录不存在")

        field = await FarmService.get_field_by_id(db, crop.field_id)
        farm = await FarmService.get_farm_by_id(db, field.farm_id)
        if not is_admin and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权删除该作物种植记录")

        raise HTTPException(409, "为保护追溯历史，批次物理删除已停用；请将批次状态更新为已收获或已中止")
