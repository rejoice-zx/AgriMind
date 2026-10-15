"""Write-time dates and quarantine checks shared by ingestion and retrieval."""
from datetime import date
from functools import lru_cache
from pathlib import Path
import json
from fastapi import HTTPException

METADATA_KEYS = frozenset({'crop', 'growth_stage', 'topic', 'disease', 'region'})
STAGE_GROUPS = (('开花期', '坐果期', '开花坐果期'), ('膨果期', '果实膨大期', '膨果转色期'))
ALL_STAGES = frozenset({'全生育期', '全生长期', '各生育期'})


def _value(item, key):
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def _text(value):
    return (value or '').strip().lower()


@lru_cache(maxsize=1)
def _province_names():
    # Reuse the checked-in administrative names; do not invent new regional aliases.
    rows = json.loads((Path(__file__).parents[1] / 'data/china_regions_2023.json').read_text(encoding='utf-8'))
    suffixes = ('维吾尔自治区', '壮族自治区', '回族自治区', '特别行政区', '自治区', '省', '市')
    names = []
    for row in rows:
        full = row['name']
        short = next((full[:-len(suffix)] for suffix in suffixes if full.endswith(suffix)), full)
        names.append((full, short))
    return tuple(names)


@lru_cache(maxsize=256)
def region_values(requested):
    """Exact place or explicit administrative parent; ambiguous partial names fail closed."""
    requested = _text(requested)
    if not requested:
        return frozenset()
    variants = {requested}
    for full, short in _province_names():
        if requested.startswith(full):
            variants.add(short + requested[len(full):])
        elif requested.startswith(short):
            variants.add(full + requested[len(short):])
    provinces = {name for pair in _province_names() for name in pair}
    accepted = {'全国', '全国通用'}
    for variant in variants:
        accepted.add(variant)
        for length in range(2, len(variant)):
            prefix = variant[:length]
            if prefix in provinces or prefix.endswith(('省', '市', '县', '区', '州', '盟')):
                accepted.add(prefix)
    return frozenset(accepted)


def stage_values(requested):
    requested = _text(requested)
    if not requested:
        return set()
    group = next((set(group) for group in STAGE_GROUPS if requested in group), {requested})
    return group | ALL_STAGES


def validate_effective_dates(start, end):
    parsed = []
    for value in (start, end):
        if value in (None, ''):
            parsed.append(None)
            continue
        try:
            result = date.fromisoformat(value)
            if result.isoformat() != value:
                raise ValueError()
        except (TypeError, ValueError):
            raise HTTPException(422, '知识有效日期须为YYYY-MM-DD；长期有效请留空') from None
        parsed.append(result)
    if all(parsed) and parsed[0] > parsed[1]:
        raise HTTPException(422, '知识失效日期不能早于生效日期')


def source_quarantined(item):
    # Protect existing rows without modifying an installed business database.
    source = (_value(item, 'source_name') or '').replace(' ', '')
    return _text(_value(item, 'source_type')) in {'', 'quarantined', 'unverified'} or ('23391' in source and '玉米' not in (_value(item, 'crop') or ''))


def metadata_matches(item, key, requested, crop=None):
    """Small, explicit vocabulary; no fuzzy widening across crops or regions."""
    actual = _text(_value(item, key))
    requested = _text(requested)
    if key == 'disease':
        crop = _text(crop or _value(item, 'crop'))
        actual = actual.removeprefix(crop)
        requested = requested.removeprefix(crop)
    if key == 'growth_stage':
        return bool(actual) and actual in stage_values(requested)
    if key == 'region':
        return bool(actual) and actual in region_values(requested)
    return bool(actual) and actual == requested


def metadata_predicates(model, filters):
    """SQL equivalent of metadata_matches, so count and pagination see the same candidates."""
    from sqlalchemy import func, case, literal
    clauses = []
    text = lambda expression: func.lower(func.trim(func.coalesce(expression, '')))
    for key, requested in filters.items():
        if key not in METADATA_KEYS:
            raise HTTPException(422, '不支持的农业知识过滤字段')
        if not requested:
            continue
        if not isinstance(requested, str):
            raise HTTPException(422, '农业知识过滤值必须是文本')
        actual, wanted = text(getattr(model, key)), _text(requested)
        if key == 'growth_stage':
            clauses.append(actual.in_(stage_values(wanted)))
        elif key == 'region':
            clauses.append(actual.in_(region_values(wanted)))
        elif key == 'disease':
            crop = text(literal(filters['crop'])) if filters.get('crop') else text(model.crop)
            strip = lambda value: case((func.substr(value, 1, func.length(crop)) == crop,
                                       func.substr(value, func.length(crop) + 1)), else_=value)
            clauses.append((actual != '') & (strip(actual) == strip(literal(wanted))))
        else:
            clauses.append((actual != '') & (actual == wanted))
    return clauses


def effective_on(item, today):
    try:
        dates = []
        for key in ('effective_from', 'effective_to'):
            value = _value(item, key)
            if value in (None, ''):
                dates.append(None)
            else:
                if not isinstance(value, str) or len(value) != 10:
                    return False
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    return False
                dates.append(parsed)
        start, end = dates
        return not (start and end and start > end) and (start is None or start <= today) and (end is None or today <= end)
    except (ValueError, TypeError):
        return False


def candidate_eligible(item, today):
    """One source/date/version gate; admission remains a candidate, never a verified permission."""
    return _value(item, 'is_current') == 1 and not source_quarantined(item) and effective_on(item, today)
