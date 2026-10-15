from fastapi import HTTPException
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from app.models import CropPlanting, CropStatusEvent
from app.services.farm_service import FarmService
from app.core.submission import submission_fingerprint
from app.utils.time import utcnow


async def cursor_page(db, model, condition, before_id=None, snapshot_id=None, limit=20):
    if snapshot_id is None:
        snapshot_id = await db.scalar(select(func.max(model.id)).where(condition)) or 0
    query = select(model).where(condition, model.id <= snapshot_id)
    if before_id is not None:
        query = query.where(model.id < before_id)
    rows = list((await db.execute(query.order_by(model.id.desc()).limit(limit + 1))).scalars().all())
    return {'items': rows[:limit], 'next_before_id': rows[limit-1].id if len(rows) > limit else None, 'snapshot_id': snapshot_id}


class CropHistoryService:
    @staticmethod
    async def list_crops(db, field_id, user, **paging):
        await FarmService.ensure_field_access(db, field_id, user)
        return await cursor_page(db, CropPlanting, CropPlanting.field_id == field_id, **paging)

    @staticmethod
    async def list_events(db, crop_id, user, **paging):
        await FarmService.ensure_crop_access(db, crop_id, user)
        return await cursor_page(db, CropStatusEvent, CropStatusEvent.crop_planting_id == crop_id, **paging)

    @staticmethod
    async def reopen(db, crop_id, payload, user):
        crop = await FarmService.ensure_crop_access(db, crop_id, user)
        field = await FarmService.get_field_by_id(db, crop.field_id)
        farm = await FarmService.get_farm_by_id(db, field.farm_id)
        if user.role != 'admin' and farm.owner_id != user.id:
            raise HTTPException(403, '只有农场所有者或管理员可以填写原因后恢复批次')
        request_id = str(payload.request_id)
        fingerprint = submission_fingerprint(field.id, {'crop_id': crop.id, 'reason': payload.reason})
        def match(event):
            if event.created_by_subject != user.auth_subject or event.request_fingerprint != fingerprint:
                raise HTTPException(409, '恢复编号已用于其他批次、账号或原因，请核对状态历史')
        existing = await db.scalar(select(CropStatusEvent).where(CropStatusEvent.request_id == request_id))
        if existing:
            match(existing)
            return {'crop': crop, 'event_id': existing.id, 'replayed': True}
        FarmService.assert_writable(field, farm)
        if crop.status not in ('harvested', 'terminated'):
            raise HTTPException(409, '仅已采收或已终止批次可以恢复，请刷新批次档案')
        event = CropStatusEvent(crop_planting_id=crop.id, previous_status=crop.status, new_status='active',
            reason=payload.reason, created_by_subject=user.auth_subject,
            actor_name=user.display_name or user.username, request_id=request_id, request_fingerprint=fingerprint)
        try:
            db.add(event)
            await db.flush()
            result = await db.execute(update(CropPlanting).where(CropPlanting.id == crop.id,
                CropPlanting.status == event.previous_status).values(status='active', updated_at=utcnow())
                .execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise HTTPException(409, '批次状态已变化，请核对历史后重新操作')
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await db.scalar(select(CropStatusEvent).where(CropStatusEvent.request_id == request_id))
            if existing is None:
                raise
            match(existing)
            await db.refresh(crop)
            return {'crop': crop, 'event_id': existing.id, 'replayed': True}
        except Exception:
            await db.rollback()
            raise
        await db.refresh(crop)
        from app.core.audit_logging import audit
        audit('crop_reopened', asset_type='crop', asset_id=crop.id, actor_id=user.id, field_id=field.id)
        return {'crop': crop, 'event_id': event.id, 'replayed': False}
