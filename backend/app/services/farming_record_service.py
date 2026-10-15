"""Farming Record Service - 农事记录、病害初诊与气象数据服务"""
import logging
from typing import Optional, List, Tuple
from datetime import date, timedelta
from sqlalchemy import select, func, and_, update
from app.utils.time import utcnow
from app.models.user import User
from app.core.audit_logging import audit
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.models.farm import Farm
from app.models.field import Field
from app.models.farming_record import FarmingRecord
from app.models.disease_record import DiseaseRecord
from app.models.weather import WeatherSnapshot
from app.models.expert_case import ExpertCase
from app.schemas.agriculture import (
    FarmingRecordCreate,
    DiseaseRecordCreate,
    WeatherSnapshotCreate,
    ExpertCaseCreate,
)

logger = logging.getLogger(__name__)


def deletion_state(record, user):
    deadline = record.created_at + timedelta(minutes=10) if record.created_at else None
    age = (utcnow() - record.created_at).total_seconds() if record.created_at else None
    allowed = user.role == 'admin' or (bool(record.created_by_subject) and record.created_by_subject == user.auth_subject and age is not None and 0 <= age < 600)
    return {'can_delete': bool(allowed and record.revoked_at is None),
            'delete_deadline_at': deadline}


class FarmingRecordService:
    """农事活动、病害诊断与气象时序服务"""

    # ==================== FarmingRecord 业务 ====================

    @staticmethod
    async def create_farming_record(
        db: AsyncSession,
        field_id: int,
        record_in: FarmingRecordCreate,
        user_id: int,
        is_admin: bool = False
    ) -> FarmingRecord:
        """为地块添加农事记录（施肥、灌溉、打药等）"""
        stmt = select(Field).where(Field.id == field_id)
        res = await db.execute(stmt)
        field = res.scalars().first()
        if not field:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="地块不存在")

        farm_stmt = select(Farm).where(Farm.id == field.farm_id)
        farm_res = await db.execute(farm_stmt)
        farm = farm_res.scalars().first()
        if not is_admin and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作该地块的农事记录")

        # 两个API入口共用关系校验；各自外键合法不能证明批次属于本田块。
        from app.services.farm_service import FarmService
        if record_in.crop_planting_id is not None:
            from app.models.crop import CropPlanting
            crop = await db.get(CropPlanting, record_in.crop_planting_id)
            if crop is None or crop.field_id != field_id:
                raise HTTPException(422, "种植批次不属于当前田块")

        # 未核验的本地候选不得补写成实际产品PHI；未知保留None。
        phi_days = record_in.safety_interval_days
        from app.core.submission import submission_fingerprint, existing_submission
        actor_subject = (await db.get(User, user_id)).auth_subject
        request_id = str(record_in.request_id) if record_in.request_id is not None else None
        fingerprint = submission_fingerprint(field_id, record_in.model_dump(mode='json')) if request_id else None
        if request_id:
            existing = await db.scalar(select(FarmingRecord).where(FarmingRecord.request_id == request_id))
            if existing is not None:
                return existing_submission(existing, actor_subject, fingerprint)
        FarmService.assert_writable(field, farm)

        record = FarmingRecord(
            created_by_subject=actor_subject,
            request_id=request_id,
            request_fingerprint=fingerprint,
            field_id=field_id,
            crop_planting_id=record_in.crop_planting_id,
            record_type=record_in.record_type,
            record_date=record_in.record_date,
            operator=record_in.operator,
            description=record_in.description,
            input_name=record_in.input_name,
            input_amount=record_in.input_amount,
            unit=record_in.unit,
            safety_interval_days=phi_days,
            attachments=record_in.attachments,
        )
        db.add(record)
        from sqlalchemy.exc import IntegrityError
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await db.scalar(select(FarmingRecord).where(FarmingRecord.request_id == request_id)) if request_id else None
            if existing is None:
                raise
            return existing_submission(existing, actor_subject, fingerprint)
        await db.refresh(record)
        return record

    @staticmethod
    async def list_farming_records(
        db: AsyncSession,
        field_id: int,
        record_type: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        skip: int = 0,
        limit: int = 100,
        record_status: str = 'active',
        crop_planting_id: Optional[int] = None
    ) -> Tuple[List[FarmingRecord], int]:
        """获取地块的农事历史记录"""
        conditions = [FarmingRecord.field_id == field_id]
        if crop_planting_id is not None:
            conditions.append(FarmingRecord.crop_planting_id == crop_planting_id)
        if record_status == 'active':
            conditions.append(FarmingRecord.revoked_at.is_(None))
        elif record_status == 'revoked':
            conditions.append(FarmingRecord.revoked_at.is_not(None))
        elif record_status != 'all':
            raise HTTPException(422, '农事记录状态无效')
        if record_type:
            conditions.append(FarmingRecord.record_type == record_type)
        if start_date:
            conditions.append(FarmingRecord.record_date >= start_date)
        if end_date:
            conditions.append(FarmingRecord.record_date <= end_date)

        count_stmt = select(func.count(FarmingRecord.id)).where(and_(*conditions))
        total = (await db.execute(count_stmt)).scalar_one()

        stmt = (
            select(FarmingRecord)
            .where(and_(*conditions))
            .order_by(FarmingRecord.record_date.desc(), FarmingRecord.id.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(stmt)
        items = list(result.scalars().all())
        return items, total

    @staticmethod
    async def delete_farming_record(
        db: AsyncSession,
        record_id: int,
        user_id: int,
        is_admin: bool = False,
        reason: str = '误录撤销'
    ) -> bool:
        """删除农事记录"""
        if not reason.strip(): raise HTTPException(422, '请填写撤销原因')
        stmt = select(FarmingRecord).where(FarmingRecord.id == record_id)
        record = (await db.execute(stmt)).scalars().first()
        if not record:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="农事记录不存在")

        field = (await db.execute(select(Field).where(Field.id == record.field_id))).scalars().first()
        if field is None: raise HTTPException(404, '所属田块不存在')
        farm = (await db.execute(select(Farm).where(Farm.id == field.farm_id))).scalars().first()
        if not farm or (not is_admin and farm.owner_id != user_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权删除该农事记录")
        user = await db.get(User, user_id)
        if user is None or not user.is_active: raise HTTPException(403, '账户不可用')
        is_admin = is_admin and user.role == 'admin'
        if not is_admin and (not record.created_by_subject or record.created_by_subject != user.auth_subject):
            raise HTTPException(403, '只能撤销本人录入的记录；历史创建者未知，请联系管理员')
        if record.revoked_at is not None: return True
        now = utcnow()
        guards = [FarmingRecord.id == record_id, FarmingRecord.revoked_at.is_(None),
            select(User.id).where(User.id == user_id, User.auth_subject == user.auth_subject, User.is_active.is_(True)).exists()]
        if is_admin:
            guards.append(select(User.id).where(User.id == user_id, User.role == 'admin').exists())
        if not is_admin:
            guards.extend([FarmingRecord.created_by_subject == user.auth_subject,
                FarmingRecord.field_id.in_(select(Field.id).join(Farm).where(Farm.owner_id == user_id)),
                FarmingRecord.created_at > now - timedelta(minutes=10), FarmingRecord.created_at <= now])
        statement = update(FarmingRecord).where(*guards).values(revoked_at=now,
            revoked_by_subject=user.auth_subject, revoke_reason=reason.strip()[:500]).returning(FarmingRecord.id)
        changed = await db.scalar(statement)
        if changed is None:
            await db.rollback()
            raise HTTPException(403, '已超过十分钟或记录状态已变化，请联系管理员协助撤销')
        await db.commit()
        audit('farming_record_revoked', actor_id=user_id, record_id=record_id)
        return True

    # ==================== DiseaseRecord 业务 ====================

    @staticmethod
    async def create_disease_record(
        db: AsyncSession,
        field_id: int,
        disease_in: DiseaseRecordCreate,
        user_id: int,
        is_admin: bool = False
    ) -> DiseaseRecord:
        """添加病害初诊/巡查记录"""
        field = (await db.execute(select(Field).where(Field.id == field_id))).scalars().first()
        if not field:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="地块不存在")

        farm = (await db.execute(select(Farm).where(Farm.id == field.farm_id))).scalars().first()
        if not is_admin and farm.owner_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作该地块的病害记录")

        if disease_in.crop_planting_id is not None:
            from app.models.crop import CropPlanting
            crop = await db.get(CropPlanting, disease_in.crop_planting_id)
            if crop is None or crop.field_id != field_id:
                raise HTTPException(400, "种植批次不属于该地块")
        record = DiseaseRecord(
            field_id=field_id,
            crop_planting_id=disease_in.crop_planting_id,
            disease_name=disease_in.disease_name,
            symptoms=disease_in.symptoms,
            severity=disease_in.severity,
            diagnosis_method=disease_in.diagnosis_method,
            diagnosis_confidence=disease_in.diagnosis_confidence,
            image_url=disease_in.image_url,
            expert_result=disease_in.expert_result,
            status=disease_in.status,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return record

    @staticmethod
    async def list_disease_records(
        db: AsyncSession,
        field_id: int,
        status_filter: Optional[str] = None
    ) -> List[DiseaseRecord]:
        """查询地块的历史病虫害记录"""
        stmt = select(DiseaseRecord).where(DiseaseRecord.field_id == field_id)
        if status_filter:
            stmt = stmt.where(DiseaseRecord.status == status_filter)
        stmt = stmt.order_by(DiseaseRecord.created_at.desc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_disease_record_by_id(db: AsyncSession, record_id: int) -> Optional[DiseaseRecord]:
        """根据 ID 查询病害记录"""
        stmt = select(DiseaseRecord).where(DiseaseRecord.id == record_id)
        return (await db.execute(stmt)).scalars().first()

    # ==================== WeatherSnapshot 业务 ====================

    @staticmethod
    async def record_weather_snapshot(
        db: AsyncSession,
        field_id: int,
        weather_in: WeatherSnapshotCreate
    ) -> WeatherSnapshot:
        """记录微气象快照"""
        field = (await db.execute(select(Field).where(Field.id == field_id))).scalars().first()
        if not field:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="地块不存在")

        snapshot = WeatherSnapshot(
            field_id=field_id,
            timestamp=weather_in.timestamp,
            temperature=weather_in.temperature,
            humidity=weather_in.humidity,
            rainfall=weather_in.rainfall,
            wind_speed=weather_in.wind_speed,
            weather=weather_in.weather,
            source=weather_in.source,
        )
        db.add(snapshot)
        await db.commit()
        await db.refresh(snapshot)
        return snapshot

    @staticmethod
    async def get_latest_weather(
        db: AsyncSession,
        field_id: int
    ) -> Optional[WeatherSnapshot]:
        """获取地块最新的一条气象快照"""
        stmt = (
            select(WeatherSnapshot)
            .where(WeatherSnapshot.field_id == field_id)
            .order_by(WeatherSnapshot.timestamp.desc())
            .limit(1)
        )
        return (await db.execute(stmt)).scalars().first()

    @staticmethod
    async def list_weather_history(
        db: AsyncSession,
        field_id: int,
        limit: int = 24
    ) -> List[WeatherSnapshot]:
        """获取地块近期的气象时序数据"""
        stmt = (
            select(WeatherSnapshot)
            .where(WeatherSnapshot.field_id == field_id)
            .order_by(WeatherSnapshot.timestamp.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # ==================== ExpertCase 业务 ====================

    @staticmethod
    async def create_expert_case(
        db: AsyncSession,
        case_in: ExpertCaseCreate,
        expert_id: int
    ) -> ExpertCase:
        """兼容入口同样要求有效认领，复用原子归档与幂等回执。"""
        from app.models.user import User
        from app.schemas.expert import ExpertResolveRequest
        from app.services.disease_review_service import DiseaseReviewService
        user = await db.get(User, expert_id)
        if user is None:
            raise HTTPException(403, "专家账号不存在")
        payload = ExpertResolveRequest(**case_in.model_dump(exclude={"disease_record_id"}))
        result = await DiseaseReviewService.resolve(db, case_in.disease_record_id, user, payload)
        return await db.get(ExpertCase, result.expert_case_id)
