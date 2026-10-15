from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.models.field_environment import FieldEnvironmentObservation
from app.services.farm_service import FarmService
from app.core.field_environment import environment_context
from app.core.submission import submission_fingerprint


class FieldEnvironmentService:
    @staticmethod
    async def history(db, field_id, user, **paging):
        from app.services.crop_history_service import cursor_page
        field = await FarmService.ensure_field_access(db, field_id, user)
        page = await cursor_page(db, FieldEnvironmentObservation, FieldEnvironmentObservation.field_id == field_id, **paging)
        from app.utils.time import utcnow
        now = utcnow()
        contexts = [environment_context(field, row, now) for row in page['items']]
        page['items'] = [{**context['latest_observation'], 'observation_status': context['observation_status']} for context in contexts]
        return page

    @staticmethod
    async def latest(db, field):
        row = (await db.execute(select(FieldEnvironmentObservation)
            .where(FieldEnvironmentObservation.field_id == field.id)
            .order_by(FieldEnvironmentObservation.observed_at.desc(), FieldEnvironmentObservation.id.desc()).limit(1))).scalars().first()
        return environment_context(field, row)

    @staticmethod
    async def create(db, field_id, payload, user):
        field = await FarmService.ensure_field_access(db, field_id, user)
        farm = await FarmService.get_farm_by_id(db, field.farm_id)
        if user.role != 'admin' and farm.owner_id != user.id:
            raise HTTPException(403, '只有所有者或管理员可以登记现场观测')
        body = payload.model_dump(mode='json')
        fingerprint = submission_fingerprint(field_id, body)
        request_id = str(payload.request_id)

        def matching(row):
            if row.created_by_subject != user.auth_subject or row.request_fingerprint != fingerprint:
                raise HTTPException(409, '观测提交编号已用于其他账号或内容，请核对历史后另建观测')
            return row

        row = await db.scalar(select(FieldEnvironmentObservation).where(FieldEnvironmentObservation.request_id == request_id))
        replayed = row is not None
        if row is not None:
            matching(row)
        else:
            FarmService.assert_writable(field, farm)
            if (field.facility_type or 'unknown') != payload.facility_type:
                raise HTTPException(409, '田块设施类型已变化，请刷新田块档案后重新核对观测')
            row = FieldEnvironmentObservation(field_id=field_id, created_by_subject=user.auth_subject,
                request_fingerprint=fingerprint, **payload.model_dump(exclude={'request_id'}), request_id=request_id)
            db.add(row)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                row = await db.scalar(select(FieldEnvironmentObservation).where(FieldEnvironmentObservation.request_id == request_id))
                if row is None:
                    raise
                matching(row)
                replayed = True
            await db.refresh(row)
        return {'observation_id': row.id, 'replayed': replayed,
                'environment': await FieldEnvironmentService.latest(db, field)}
