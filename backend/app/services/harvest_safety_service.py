"""Deterministic record-based PHI warnings; never a product registration approval."""
from datetime import timedelta
from sqlalchemy import select, or_
from app.models.farming_record import FarmingRecord, is_spraying_record
from app.models.crop import CropPlanting
from app.core.pesticide_standards import find_all_banned_pesticides
from app.core.weather_policy import agriculture_today


async def harvest_safety(db, field_id, crop_planting_id=None):
    crop = await db.get(CropPlanting, crop_planting_id) if crop_planting_id else None
    if crop is None or crop.field_id != field_id:
        return {'status': 'unknown', 'can_approve': False, 'reasons': ['请选择本田块的明确种植批次'], 'records': []}
    # Include unbound records since planting: they cannot be silently assumed unrelated.
    rows = (await db.scalars(select(FarmingRecord).where(FarmingRecord.field_id == field_id,
        FarmingRecord.revoked_at.is_(None), or_(FarmingRecord.crop_planting_id == crop.id,
        (FarmingRecord.crop_planting_id.is_(None)) & (FarmingRecord.record_date >= crop.planting_date))))).all()
    reasons, records, dates = [], [], []
    for row in rows:
        # Banned names can appear in any record type or in the free-text description (BUG-031).
        banned = find_all_banned_pesticides(f"{row.input_name or ''} {row.description or ''}")
        if banned and not is_spraying_record(row.record_type):
            reasons.append(f'记录#{row.id}含禁限用成分名称：' + '、'.join(banned))
        if not is_spraying_record(row.record_type):
            continue
        phi = row.safety_interval_days
        if banned: reasons.append('记录含番茄禁限用成分：' + '、'.join(banned))
        if phi is None or phi < 0: reasons.append(f'记录#{row.id}安全间隔期未知')
        else:
            try: dates.append(row.record_date + timedelta(days=phi))
            except (OverflowError, ValueError): reasons.append(f'记录#{row.id}安全间隔期超出可计算范围')
        if row.crop_planting_id is None: reasons.append(f'记录#{row.id}未绑定批次，需核对是否适用')
        records.append({'id': row.id, 'date': row.record_date.isoformat(), 'input_name': row.input_name,
                        'safety_interval_days': phi, 'banned_names': banned})
    earliest = max(dates) if dates else None
    if earliest and earliest > agriculture_today(): reasons.append('尚未达到记录安全间隔期推算日期')
    if earliest and crop.expected_harvest_date and earliest > crop.expected_harvest_date:
        reasons.append('预计采收日期早于记录安全间隔期推算日期')
    return {'status': 'blocked' if reasons else 'needs_label_review', 'can_approve': False,
            'earliest_from_recorded_intervals': earliest.isoformat() if earliest else None,
            'expected_harvest_date': crop.expected_harvest_date.isoformat() if crop.expected_harvest_date else None,
            'reasons': reasons + ['记录值尚不能替代产品标签、适用作物和用法核验'], 'records': records}
