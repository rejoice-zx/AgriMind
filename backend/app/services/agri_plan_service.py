"""Generate reviewable agricultural checklists from actual crop and weather evidence."""
import logging
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List
from sqlalchemy import select, desc, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException
from app.models.field import Field
from app.models.farm import Farm
from app.models.crop import CropPlanting
from app.models.farming_record import FarmingRecord, is_spraying_record, is_fertilizing_record, IRRIGATION_TYPES
from app.models.user import User
from app.models.plan_execution import PlanExecution
from app.models.agri_plan_snapshot import AgriPlanSnapshot
from app.models.settings import SystemSettings
from app.utils.time import utcnow
from app.schemas.agriculture import AgriPlanRequest, AgriPlanResponse, PlanItem
from app.services.weather_service import WeatherService
from app.services.plan_confirmation import issue_confirmation, verify_confirmation, execution_fingerprint
from app.core.weather_policy import agriculture_today, evaluate_spray_weather

logger = logging.getLogger(__name__)


@dataclass
class PlanApplyResult:
    record_ids: List[int]
    created_count: int


class AgriPlanService:
    @staticmethod
    async def _field(db, field_id, user_id, user_role, write=False):
        field = (await db.execute(select(Field).where(Field.id == field_id))).scalars().first()
        if not field:
            raise HTTPException(404, '田块不存在')
        farm = (await db.execute(select(Farm).where(Farm.id == field.farm_id))).scalars().first()
        if not farm:
            raise HTTPException(404, '所属农场不存在')
        readers = ('admin',) if write else ('admin', 'expert')
        if user_role not in readers and farm.owner_id != user_id:
            raise HTTPException(403, '只有田块所有者或管理员可以确认执行记录' if write else '无权限操作此田块')
        from app.services.farm_service import FarmService
        FarmService.assert_writable(field, farm)
        return field

    @staticmethod
    async def _planting(db, field_id, planting_id=None):
        query = select(CropPlanting).where(CropPlanting.field_id == field_id, CropPlanting.status == 'active')
        if planting_id is not None:
            query = query.where(CropPlanting.id == planting_id)
        candidates = (await db.scalars(query.order_by(desc(CropPlanting.planting_date)).limit(2))).all()
        if planting_id is None and len(candidates) > 1:
            raise HTTPException(422, '此田块有多个活跃批次，请明确选择作物批次')
        return candidates[0] if candidates else None

    @classmethod
    async def generate_plan(cls, db: AsyncSession, request: AgriPlanRequest, user_id: int, user_role: str, _attempt=0) -> AgriPlanResponse:
        field = await cls._field(db, request.field_id, user_id, user_role)
        planting = await cls._planting(db, field.id, request.crop_planting_id)
        if request.crop_planting_id is not None and planting is None:
            raise HTTPException(404, '未找到此田块的活跃种植批次')
        today = agriculture_today()
        if not planting:
            return AgriPlanResponse(field_id=field.id, field_name=field.name, crop='未录入', growth_stage='未录入',
                start_date=today.isoformat(), end_date=(today + timedelta(days=request.days - 1)).isoformat(),
                summary='未录入活跃作物，暂不生成检查建议。', items=[], total_tasks=0, weather_summary='暂无计划天气依据')
        snapshot = (await db.execute(select(AgriPlanSnapshot).where(
            AgriPlanSnapshot.crop_planting_id == planting.id).execution_options(populate_existing=True))).scalar_one_or_none()
        farm = await db.get(Farm, field.farm_id)
        record_revision = (await db.execute(select(func.count(FarmingRecord.id), func.max(FarmingRecord.updated_at))
            .where(FarmingRecord.field_id == field.id))).one()
        # Read configuration revision only; never persist provider credentials in a plan.
        settings_revision = (await db.execute(select(SystemSettings.updated_at)
            .where(SystemSettings.key == 'agri_ai_config'))).scalar_one_or_none()
        from app.models.field_environment import FieldEnvironmentObservation
        environment_revision = (await db.execute(select(func.max(FieldEnvironmentObservation.id))
            .where(FieldEnvironmentObservation.field_id == field.id))).scalar_one_or_none()
        inputs = ['inspection-v3-environment', environment_revision, field.facility_type, field.updated_at, field.latitude, field.longitude, farm.updated_at,
                  farm.latitude, farm.longitude, planting.updated_at, planting.crop, planting.growth_stage,
                  planting.planting_date, planting.expected_harvest_date, *record_revision, settings_revision]
        input_hash = hashlib.sha256(json.dumps(inputs, default=str, ensure_ascii=False).encode()).hexdigest()
        now = utcnow()
        ttl = 60 if snapshot and snapshot.payload.get('weather_degraded') else 600
        weather_expired = False
        if snapshot:
            # A plan must not extend the lifetime of weather already served from a cache.
            for key, max_age in (('weather_fetched_at', ttl), ('weather_observed_at', 7200), ('environment_observed_at', 3600)):
                timestamp = snapshot.payload.get(key)
                if timestamp:
                    try:
                        age = (now - datetime.fromisoformat(timestamp.replace('Z', '+00:00'))).total_seconds()
                        weather_expired |= age >= max_age or age < -300
                    except (ValueError, TypeError):
                        weather_expired = True
        needs_update = (snapshot is None or request.force_refresh or snapshot.input_hash != input_hash
                        or snapshot.payload['start_date'] != today.isoformat()
                        or (now - snapshot.evaluated_at).total_seconds() >= ttl or weather_expired)
        if needs_update:
            reason = ('首次生成' if snapshot is None else '手动刷新核查' if request.force_refresh else
                      '农情、履历或配置变化' if snapshot.input_hash != input_hash else
                      '跨天延长计划窗口' if snapshot.payload['start_date'] != today.isoformat() else '定期核查天气')
            weather = await WeatherService.get_field_weather(db, field, 14, **({'force_refresh': True} if request.force_refresh else {}))
            recent = list((await db.execute(select(FarmingRecord).where(
                FarmingRecord.field_id == field.id, FarmingRecord.crop_planting_id == planting.id,
                FarmingRecord.revoked_at.is_(None), FarmingRecord.record_date <= today)
                .order_by(desc(FarmingRecord.record_date), desc(FarmingRecord.id)).limit(30))).scalars())
            # An inspection receipt is not evidence of actual irrigation/fertilizer application.
            latest = {}
            for record in recent:
                if isinstance(record.attachments, dict) and record.attachments.get('from_agri_plan'):
                    continue
                kind = ('pest_control' if is_spraying_record(record.record_type) else
                        'fertilization' if is_fertilizing_record(record.record_type) else
                        'irrigation' if record.record_type in IRRIGATION_TYPES else record.record_type)
                latest.setdefault(kind, record.record_date.isoformat())
            anchor = snapshot.anchor_date if snapshot else today
            plan = cls._build_plan(field, planting, weather, today, anchor, latest)
            old_items = {item['date']: item for item in snapshot.payload['items']} if snapshot else {}
            # Completed/revoked receipts retain their original task facts across reassessment.
            old_fingerprints = [execution_fingerprint(PlanItem.model_validate(item), field.id) for item in old_items.values()]
            receipts = set((await db.execute(select(PlanExecution.fingerprint).where(
                PlanExecution.field_id == field.id, PlanExecution.fingerprint.in_(old_fingerprints)))).scalars()) if old_fingerprints else set()
            for index, item in enumerate(plan.items):
                old = old_items.get(item.date)
                if old and execution_fingerprint(PlanItem.model_validate(old), field.id) in receipts:
                    plan.items[index] = PlanItem.model_validate(old)
            changes = []
            for item in plan.items:
                old = old_items.get(item.date)
                if old is None:
                    changes.append(f'{item.date}：补充检查建议')
                elif old != item.model_dump(mode='json'):
                    changes.append(f'{item.date}：更新农情依据或风险提示，任务日期保持不变')
            payload = plan.model_dump(mode='json')
            payload.update(version=(snapshot.version + 1 if snapshot else 1), generated_at=now.isoformat(),
                           evaluated_at=now.isoformat(), update_reason=reason, changes=changes,
                           weather_fetched_at=weather.get('fetched_at'),
                           weather_observed_at=(weather.get('current') or {}).get('timestamp'),
                           weather_degraded=bool(weather.get('is_fallback') or weather.get('status') != 'online'))
            environment = weather.get('environment') or {}
            payload['environment_observed_at'] = ((environment.get('latest_observation') or {}).get('observed_at')
                if environment.get('observation_status') == 'recent' else None)
            if snapshot:
                result = await db.execute(update(AgriPlanSnapshot).where(
                    AgriPlanSnapshot.crop_planting_id == planting.id, AgriPlanSnapshot.version == snapshot.version)
                    .values(input_hash=input_hash, version=payload['version'], evaluated_at=now, payload=payload)
                    .execution_options(synchronize_session=False))
                saved = result.rowcount == 1
            else:
                try:
                    async with db.begin_nested():
                        db.add(AgriPlanSnapshot(crop_planting_id=planting.id, field_id=field.id, anchor_date=anchor,
                            input_hash=input_hash, version=1, evaluated_at=now, payload=payload))
                        await db.flush()
                    saved = True
                except IntegrityError:
                    saved = False
            await db.commit()
            if not saved:
                if _attempt >= 2:
                    raise HTTPException(409, '计划正在更新，请稍后重试')
                return await cls.generate_plan(db, request.model_copy(update={'force_refresh': False}), user_id, user_role, _attempt + 1)
        else:
            payload = snapshot.payload
        response = AgriPlanResponse.model_validate(payload)
        response.end_date = (today + timedelta(days=request.days - 1)).isoformat()
        response.items = [item for item in response.items if today.isoformat() <= item.date <= response.end_date]
        response.total_tasks = len(response.items)
        response.changes = [change for change in response.changes if today.isoformat() <= change[:10] <= response.end_date]
        response.summary = f'{len(response.items)} 项农事检查建议；请结合现场情况确认，施药方案需专家复核。'
        await cls._restore_execution_state(db, field.id, response.items)
        for item in response.items:
            item.proof = issue_confirmation(item, field.id, user_id)
        return response

    @staticmethod
    def _build_plan(field, planting, weather, today, anchor, latest):
        from app.core.field_environment import environment_context
        environment = weather.get('environment') or environment_context(field)
        days = 14  # Dashboard and calendar share exactly the same rolling window.
        forecasts = {item['date']: item for item in weather.get('forecasts', [])}
        items = []
        # These are review checklists, not unverified drug prescriptions or national standards.
        routines = [
            ('field_management', '巡视长势并记录异常', '观察植株和叶片，将异常及现场照片记入田块档案。'),
            ('irrigation', '核查灌溉需求与设施', '核实土壤、灌溉设施和历史用水记录，实际用水量由现场确认。'),
            ('pest_control', '植保施药方案待复核', '先核实病虫害、登记药剂、适用作物与产品标签，提交农技专家复核后记录实际施药。'),
            ('fertilization', '核查施肥需求', '结合土壤、长势和历史投入品核查需求，避免直接按固定剂量施肥。'),
        ]
        if planting:
            for i in range(days):
                day = (today + timedelta(days=i)).isoformat()
                forecast = forecasts.get(day, {})
                check = (evaluate_spray_weather(forecast) if environment['outdoor_weather_applicable'] else
                         {'reasons': ['设施环境须现场及专家复核，室外阈值不直接用于设施内判定'], 'reason_code': 'facility_review'})
                kind, action, description = routines[((today - anchor).days + i) % len(routines)]
                is_spray = kind == 'pest_control'
                reasons = list(check['reasons'])
                code = check['reason_code']
                if is_spray:
                    reasons.append('药剂与安全间隔期尚未核验，需要专家复核，系统不自动生成施药处方')
                    if code == 'weather_clear':
                        code = 'expert_review'
                context = f"已录入作物 {planting.crop} / {planting.growth_stage or '阶段未录入'}；此为检查建议，频次并非农艺处方"
                if environment:
                    context += '；' + environment['facility_label'] + '；' + environment['notice']
                if kind in latest:
                    context += f'；最近同类农事记录日期：{latest[kind]}，请核对实际操作，避免重复投入'
                reason = '；'.join([context, *reasons])
                item = PlanItem(date=day, record_type=kind, action=action, description=description,
                    reason=reason, source='系统检查清单（非农艺处方，非国家标准）',
                    risk_notes='；'.join(reasons) or None, is_spray=is_spray,
                    # These non-spray items are inspections/needs checks, not weather-dependent prescriptions.
                    can_proceed=not is_spray, reason_code=code,
                    crop_planting_id=planting.id)
                items.append(item)
        return AgriPlanResponse(field_id=field.id, field_name=field.name,
            crop=planting.crop, growth_stage=planting.growth_stage or '未录入',
            start_date=today.isoformat(), end_date=(today+timedelta(days=days-1)).isoformat(), items=items,
            summary='农事检查建议，非农艺处方。',
            weather_summary=weather.get('summary') or '暂无有效天气数据', total_tasks=len(items))

    @staticmethod
    async def _restore_execution_state(db, field_id, items):
        if items:
            fingerprints = {execution_fingerprint(item, field_id): item for item in items}
            rows = await db.execute(select(PlanExecution.fingerprint, PlanExecution.record_id,
                FarmingRecord.id, FarmingRecord.revoked_at)
                .outerjoin(FarmingRecord, (FarmingRecord.id == PlanExecution.record_id) &
                           (FarmingRecord.field_id == field_id))
                .where(PlanExecution.field_id == field_id, PlanExecution.fingerprint.in_(fingerprints)))
            for fingerprint, record_id, saved_id, revoked_at in rows:
                item = fingerprints[fingerprint]
                item.execution_record_id = record_id
                item.execution_status = 'unavailable' if saved_id is None else 'revoked' if revoked_at else 'completed'

    @classmethod
    async def apply_plan_items(cls, db: AsyncSession, field_id: int, items: List[PlanItem], operator: str, user_id: int, user_role: str) -> PlanApplyResult:
        field = await cls._field(db, field_id, user_id, user_role, write=True)
        planting_ids = {item.crop_planting_id for item in items}
        if len(planting_ids) != 1 or None in planting_ids:
            raise HTTPException(409, '请一次确认同一明确作物批次的计划，刷新后再试')
        planting = await cls._planting(db, field_id, next(iter(planting_ids)))
        if planting is None:
            raise HTTPException(409, '请先录入活跃种植批次')
        today = agriculture_today().isoformat()
        # Validate the entire request before any writes; browser flags are not authorization.
        for item in items:
            if not verify_confirmation(item, field_id, user_id):
                raise HTTPException(409, '计划已过期或内容被修改，请刷新计划后确认')
            if item.date != today:
                raise HTTPException(409, '只能记录今天已实际完成的事项，未来计划不能写为执行履历')
            if not item.can_proceed or item.is_spray or is_spraying_record(item.record_type):
                raise HTTPException(409, '此项尚未通过复核，请使用农事记录填写实际操作与核验信息')
            if item.crop_planting_id != planting.id:
                raise HTTPException(409, '活跃作物已变化，请重新生成计划')
        fingerprints = [execution_fingerprint(item, field_id) for item in items]
        receipts = list((await db.execute(select(PlanExecution).where(PlanExecution.fingerprint.in_(fingerprints)))).scalars().all())
        existing = {receipt.fingerprint: receipt for receipt in receipts}
        # A retry of completed records need not contact weather again.
        if any(fp not in existing for fp in fingerprints):
            weather = await WeatherService.get_field_weather(db, field, 1)
            # Retain the available weather evidence, but do not deny recording a
            # completed non-spray checklist because spray weather is unsuitable.
        ids = []
        created_count = 0
        try:
            for item, fingerprint in zip(items, fingerprints):
                if fingerprint in existing:
                    receipt = existing[fingerprint]
                    saved_record = await db.get(FarmingRecord, receipt.record_id) if receipt.record_id else None
                    if saved_record is None or saved_record.revoked_at is not None:
                        raise HTTPException(409, '该事项已有确认记录，请核查历史履历')
                    ids.append(receipt.record_id)
                    continue
                receipt = PlanExecution(fingerprint=fingerprint, field_id=field_id)
                db.add(receipt)
                await db.flush()  # Unique receipt reserves this operation across concurrent requests.
                record = FarmingRecord(field_id=field_id, crop_planting_id=planting.id,
                    created_by_subject=(await db.get(User, user_id)).auth_subject,
                    record_type='inspection', record_date=date.fromisoformat(item.date), operator=operator,
                    description=f'【人工确认已完成】{item.action}。{item.description}',
                    input_name=None, input_amount=None, unit=None, safety_interval_days=None,
                    attachments={'source': item.source, 'from_agri_plan': True, 'weather_evidence': weather})
                db.add(record)
                await db.flush()
                receipt.record_id = record.id
                created_count += 1
                existing[fingerprint] = receipt
                ids.append(record.id)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, '同一事项已被其他请求确认，请刷新履历') from None
        except Exception:
            await db.rollback()
            raise
        return PlanApplyResult(record_ids=ids, created_count=created_count)
