"""Field context is separate from outdoor provider data and never grants work permission."""
from datetime import datetime, timezone

FACILITY_LABELS = {'unknown': '设施类型未核实', 'open_field': '露地', 'greenhouse': '温室/大棚', 'tunnel': '拱棚/覆盖栽培', 'other': '其他设施'}
OBSERVATION_MAX_AGE_MINUTES = 60  # Application freshness limit; not an agronomic standard.


def environment_context(field, observation=None, now=None):
    facility = getattr(field, 'facility_type', None) or 'unknown'
    if facility not in FACILITY_LABELS:
        facility = 'unknown'
    now = now or datetime.now(timezone.utc)
    status, detail = 'missing', None
    if observation is not None:
        observed = observation.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = (now - observed).total_seconds()
        expected_scope = 'onsite_outdoor' if facility == 'open_field' else 'inside_facility'
        status = ('context_changed' if observation.facility_type != facility else
                  'scope_mismatch' if facility != 'unknown' and observation.measurement_scope != expected_scope else
                  'future' if age < 0 else 'stale' if age > OBSERVATION_MAX_AGE_MINUTES * 60 else 'recent')
        detail = {key: getattr(observation, key) for key in ('id', 'facility_type', 'measurement_scope',
            'temperature', 'humidity', 'ventilation', 'covering', 'notes')}
        detail.update(observed_at=observed.isoformat(), recorded_at=observation.created_at.isoformat(),
                      source='manual_observation')
    if facility == 'open_field':
        notice = '室外预报仅作露地天气风险参考，不能代替作业时的现场风雨核查，也不是施药许可。'
    elif facility == 'unknown':
        notice = '设施类型未核实，不能将室外温湿度、降雨或风速作为田块内作业条件。'
    else:
        notice = '室外预报不能代表设施内温湿度、风雨或通风状况；不直接套用露地阈值，需现场及专家复核。'
    statuses = {'missing': '尚无现场观测', 'stale': '最近观测已超过本系统60分钟参考时限',
                'context_changed': '观测记录的设施类型已变化', 'scope_mismatch': '观测位置与当前设施环境不匹配',
                'future': '观测时间在未来，不能作为当前依据', 'recent': '近期人工观测，仅表示录入值，未核验传感器或持续有效性'}
    return {'facility_type': facility, 'facility_label': FACILITY_LABELS[facility],
            'outdoor_weather_applicable': facility == 'open_field', 'observation_status': status,
            'observation_max_age_minutes': OBSERVATION_MAX_AGE_MINUTES, 'latest_observation': detail,
            'notice': notice + statuses[status] + '。', 'can_approve_operation': False}


def bind_environment_weather(weather, context):
    """Copy envelopes: provider caches remain coordinate-scoped and field-independent."""
    safety = dict(weather.get('spray_safety') or {})
    risks = list(safety.get('spray_risks') or [])
    safety['spray_risks'] = risks
    outdoor_assessment = {**safety, 'spray_risks': list(risks)}
    if not context['outdoor_weather_applicable']:
        safety.update(is_suitable_to_spray=False, spray_risks=[context['notice']],
                      has_rain_soon=False, high_temp_risk=False, strong_wind_risk=False,
                      assessment_scope='facility_review_required')
    else:
        safety['assessment_scope'] = 'open_field_weather_risk_only'
    return {**weather, 'environment': context, 'spray_safety': safety,
            'outdoor_weather_assessment': outdoor_assessment}
