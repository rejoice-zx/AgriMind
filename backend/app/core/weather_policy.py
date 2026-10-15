"""Shared precautionary weather rules. Thresholds are application policy, not a certified standard."""
import math
from datetime import datetime, timedelta, timezone

AGRI_TIMEZONE = timezone(timedelta(hours=8))
POLICY_VERSION = "precaution-v1"


def agriculture_today():
    return datetime.now(AGRI_TIMEZONE).date()


def finite_number(value, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return float(value)


def evaluate_spray_weather(forecast):
    """Never turn missing measurements into zero or claim a missing forecast is safe."""
    values = {
        "rainfall_mm": finite_number(forecast.get("rainfall_mm"), 0),
        "rain_prob": finite_number(forecast.get("rain_prob"), 0, 100),
        "wind_speed": finite_number(forecast.get("wind_speed"), 0),
        "temp_max": finite_number(forecast.get("temp_max"), -100, 70),
    }
    missing = [name for name, value in values.items() if value is None]
    reasons = []
    labels = {"rainfall_mm": "日降水量", "rain_prob": "日降雨概率", "wind_speed": "日最大风速", "temp_max": "日最高温"}
    if missing:
        reasons.append("缺少有效预报：" + "、".join(labels[name] for name in missing))
    rain = (values["rainfall_mm"] is not None and values["rainfall_mm"] >= 3) or (values["rain_prob"] is not None and values["rain_prob"] >= 50)
    wind = values["wind_speed"] is not None and values["wind_speed"] >= 4
    heat = values["temp_max"] is not None and values["temp_max"] >= 32
    if values["rainfall_mm"] is not None and values["rainfall_mm"] >= 3:
        reasons.append(f"日降水量 {values['rainfall_mm']:g}mm ≥ 3mm")
    if values["rain_prob"] is not None and values["rain_prob"] >= 50:
        reasons.append(f"日降雨概率 {values['rain_prob']:g}% ≥ 50%")
    if wind:
        reasons.append(f"日最大风速 {values['wind_speed']:g}m/s ≥ 4m/s")
    if heat:
        reasons.append(f"日最高温 {values['temp_max']:g}℃ ≥ 32℃")
    return {"can_proceed": not reasons, "reason_code": "weather_unknown" if missing else "weather_risk" if reasons else "weather_clear",
            "reasons": reasons, "has_rain_soon": bool(rain), "strong_wind_risk": bool(wind), "high_temp_risk": bool(heat),
            "policy_version": POLICY_VERSION}
