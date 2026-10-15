# -*- coding: utf-8 -*-
"""
AgriMind 真实气象时序与预测服务 (WeatherService)

特性:
1. 对接 Open-Meteo 全球高精度开放气象 API (基于经纬度获取小时级/日级真实天气，免 Key)
2. 真实输出未来 7 天降雨概率、降水量、极限温湿度与风速
3. 使用统一的系统保守阈值研判风险；不将未核验阈值声称为国家标准。
"""

import time
import copy
import logging
from datetime import date, timedelta
from typing import Dict, Any, Optional, Tuple
import httpx
from app.core.weather_policy import agriculture_today, evaluate_spray_weather, finite_number, AGRI_TIMEZONE
from app.schemas.weather import FieldWeatherResponse
from app.services.weather_requests import share_weather_request
from datetime import datetime


logger = logging.getLogger(__name__)

# 真实气象数据进程内短期缓存 (TTL: 600秒)，避免高频重复请求外网
_WEATHER_CACHE: Dict[Tuple[float, float, int, str], Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 600.0


class WeatherService:
    """真实气象服务客户端"""

    @staticmethod
    async def get_real_weather(
        latitude: float = 36.88,
        longitude: float = 118.74,
        days: int = 7,
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        根据经纬度获取真实气象数据与未来预报。当前系统生产环境采用 Open-Meteo 高精度气象模型。
        """
        days = max(1, min(days, 14))
        if finite_number(latitude, -90, 90) is None or finite_number(longitude, -180, 180) is None:
            return WeatherService._fallback_safe_weather(days, "location_missing", "未绑定有效经纬度，不能查询本田块天气")
        cache_key = (latitude, longitude, days, agriculture_today().isoformat())
        now_ts = time.time()
        if not force_refresh and cache_key in _WEATHER_CACHE:
            expire_at, cached_data = _WEATHER_CACHE[cache_key]
            if now_ts < expire_at:
                return copy.deepcopy(cached_data)

        async def fetch():
            res = await WeatherService._fetch_open_meteo(latitude, longitude, days)
            if res:
                ttl = _CACHE_TTL_SECONDS if not res.get("is_fallback", False) else 60.0
                observed = res.get('current', {}).get('timestamp')
                if observed and res['current'].get('temperature') is not None:
                    remaining = 7200 - (datetime.now(AGRI_TIMEZONE) - datetime.fromisoformat(observed)).total_seconds()
                    ttl = min(ttl, max(0, remaining))
                _WEATHER_CACHE[cache_key] = (time.time() + ttl, copy.deepcopy(res))
                while len(_WEATHER_CACHE) > 128:
                    _WEATHER_CACHE.pop(next(iter(_WEATHER_CACHE)))
            return res

        return await share_weather_request(('open_meteo', cache_key, force_refresh), fetch)

    @staticmethod
    async def _fetch_open_meteo(latitude: float, longitude: float, days: int = 7) -> Dict[str, Any]:
        """从 Open-Meteo 获取真实经纬度气象"""
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}&longitude={longitude}"
            f"&current=temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max"
            f"&timezone=Asia%2FShanghai"
            f"&wind_speed_unit=ms&forecast_days={days}"
        )
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return WeatherService._parse_open_meteo_response(data, days)
                else:
                    logger.warning("Open-Meteo 响应异常: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning("Open-Meteo 请求失败: %s", type(e).__name__)

        # 网络不可用时保留未知值，不生成替代气象事实。
        return WeatherService._fallback_safe_weather(days)

    @staticmethod
    def _parse_open_meteo_response(data: Dict[str, Any], days: int) -> Dict[str, Any]:
        current = data.get("current") or {}
        daily = data.get("daily") or {}
        current_units = data.get("current_units") or {}
        daily_units = data.get("daily_units") or {}

        def wmo_to_desc(code: int) -> str:
            if code == 0:
                return "晴朗"
            elif code in (1, 2, 3):
                return "多云/阴天"
            elif code in (45, 48):
                return "有雾"
            elif code in (51, 53, 55, 61, 63, 65):
                return "小雨/阵雨"
            elif code in (66, 67, 80, 81, 82):
                return "中到大雨"
            elif code in (95, 96, 99):
                return "雷暴雨"
            return "未知" if code is None else f"天气代码 {code}"

        def number(value, units, name, minimum=None, maximum=None):
            value = finite_number(value, minimum, maximum)
            if value is None:
                return None
            unit = units.get(name)
            if "wind_speed" in name:
                if unit in ("m/s", "ms"):
                    return value
                if unit in ("km/h", "kmh"):
                    return round(value / 3.6, 3)
                return None
            expected = "%" if "humidity" in name or "probability" in name else "mm" if "precipitation" in name else "°C"
            return value if unit == expected else None

        def at(name, index):
            values = daily.get(name) or []
            return values[index] if index < len(values) else None

        forecasts = []
        seen = set()
        for i, day in enumerate((daily.get("time") or [])[:days]):
            try:
                parsed = date.fromisoformat(day)
            except (TypeError, ValueError):
                continue
            if day in seen:
                continue
            seen.add(day)
            forecasts.append({
                "date": parsed.isoformat(), "weather": wmo_to_desc(at("weather_code", i)),
                "temp_max": number(at("temperature_2m_max", i), daily_units, "temperature_2m_max", -100, 70),
                "temp_min": number(at("temperature_2m_min", i), daily_units, "temperature_2m_min", -100, 70),
                "rainfall_mm": number(at("precipitation_sum", i), daily_units, "precipitation_sum", 0),
                "rain_prob": number(at("precipitation_probability_max", i), daily_units, "precipitation_probability_max", 0, 100),
                "wind_speed": number(at("wind_speed_10m_max", i), daily_units, "wind_speed_10m_max", 0),
                "humidity": None,
            })
        by_date = {f["date"]: f for f in forecasts}
        checks = []
        risks = []
        for i in range(min(2, days)):
            day = (agriculture_today() + timedelta(days=i)).isoformat()
            check = evaluate_spray_weather(by_date.get(day, {}))
            checks.append(check)
            risks.extend(f"{day}：{reason}" for reason in check["reasons"])
        incomplete = any(evaluate_spray_weather(by_date.get((agriculture_today() + timedelta(days=i)).isoformat(), {}))["reason_code"] == "weather_unknown" for i in range(days))
        temperature = number(current.get("temperature_2m"), current_units, "temperature_2m", -100, 70)
        timestamp = current.get("time")
        try:
            observed = datetime.fromisoformat(timestamp)
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=AGRI_TIMEZONE)
            timestamp = observed.isoformat()
            if abs((datetime.now(AGRI_TIMEZONE) - observed).total_seconds()) > 7200:
                temperature = None
                incomplete = True
        except (TypeError, ValueError):
            timestamp = None
            temperature = None
            incomplete = True
        result = {
            "status": "partial" if incomplete else "online", "is_fallback": False,
            "fetched_at": datetime.now(AGRI_TIMEZONE).isoformat(),
            "summary": "气象预报不完整，请核实缺失数据" if incomplete else "；".join(risks) if risks else "未来48小时未触发系统气象预警（不代表作业已获批准）",
            "current": {
                "temperature": temperature,
                "humidity": number(current.get("relative_humidity_2m"), current_units, "relative_humidity_2m", 0, 100) if temperature is not None else None,
                "rainfall": number(current.get("precipitation"), current_units, "precipitation", 0) if temperature is not None else None,
                "wind_speed": number(current.get("wind_speed_10m"), current_units, "wind_speed_10m", 0) if temperature is not None else None,
                "weather": wmo_to_desc(current.get("weather_code")),
                "source": "Open-Meteo 天气模型（室外）", "timestamp": timestamp,
            },
            "forecasts": forecasts,
            "spray_safety": {
                "has_rain_soon": any(c["has_rain_soon"] for c in checks),
                "high_temp_risk": any(c["high_temp_risk"] for c in checks),
                "strong_wind_risk": any(c["strong_wind_risk"] for c in checks),
                "is_offline_warning": any(c["reason_code"] == "weather_unknown" for c in checks),
                "spray_risks": risks,
                "is_suitable_to_spray": bool(checks) and all(c["can_proceed"] for c in checks),
            }
        }
        if any(result["current"].get(name) is None for name in ("temperature", "wind_speed", "rainfall")):
            message = "当前天气数据缺失或过期，需要人工核实现场"
            result["status"] = "partial"
            result["summary"] = message + ("；" + "；".join(risks) if risks else "")
            result["spray_safety"]["spray_risks"].append(message)
            result["spray_safety"]["is_suitable_to_spray"] = False
            result["spray_safety"]["is_offline_warning"] = True
        return FieldWeatherResponse.model_validate(result).model_dump()

    @staticmethod
    def _fallback_safe_weather(days: int, state="offline", reason="气象服务离线，无法确认风雨和高温风险") -> Dict[str, Any]:
        """网络不可用时的客观安全降级（保守防护，严禁虚假兜底晴好或适宜）"""
        today = agriculture_today()
        forecasts = []
        for i in range(days):
            d = today + timedelta(days=i)
            forecasts.append({
                "date": d.strftime("%Y-%m-%d"),
                "weather": "气象离线未连通",
                "temp_max": None,
                "temp_min": None,
                "rainfall_mm": None,
                "rain_prob": None,
                "wind_speed": None,
                "humidity": None
            })
        return {
            "is_fallback": True,
            "status": state, "summary": reason, "source": "暂无有效天气来源",
            "fetched_at": datetime.now(AGRI_TIMEZONE).isoformat(),
            "current": {
                "temperature": None,
                "humidity": None,
                "rainfall": None,
                "wind_speed": None,
                "weather": "气象服务离线/暂无数据",
                "source": "Offline_Fallback"
            },
            "forecasts": forecasts,
            "spray_safety": {
                "has_rain_soon": False,
                "high_temp_risk": False,
                "strong_wind_risk": False,
                "is_offline_warning": True,
                "spray_risks": [
                    reason,
                    "依据农药安全使用准则，无法确认风雨风险时建议暂缓施药，人工核实田间实况后再行决策。"
                ],
                "is_suitable_to_spray": False
            }
        }

    @staticmethod
    async def get_field_weather(db, field, days=7, force_refresh=False):
        """Resolve an actual field/farm coordinate pair; never substitute a demonstration location."""
        from sqlalchemy import select
        from app.models.farm import Farm
        from app.services.settings_service import SettingsService
        farm = (await db.execute(select(Farm).where(Farm.id == field.farm_id))).scalars().first()
        coordinates = None
        coordinate_source = None
        for obj, source in ((field, "field"), (farm, "farm")):
            if obj is not None and finite_number(obj.latitude, -90, 90) is not None and finite_number(obj.longitude, -180, 180) is not None:
                coordinates = (obj.latitude, obj.longitude)
                coordinate_source = source
                break
        config = await SettingsService.get_agri_ai_config(db)
        if coordinates is None:
            result = WeatherService._fallback_safe_weather(days, "location_missing", "田块和农场均未绑定有效经纬度")
        elif config.weather_provider == "custom":
            from app.services.custom_weather_service import CustomWeatherService
            result = await CustomWeatherService.fetch(config.weather_custom, config.weather_api_key, coordinates[0], coordinates[1], days,
                **({'force_refresh': True} if force_refresh else {}))
        elif config.weather_provider != "open_meteo":
            result = WeatherService._fallback_safe_weather(days, "unsupported_provider", "所选气象提供商尚未接入")
        else:
            if force_refresh:
                result = await WeatherService.get_real_weather(latitude=coordinates[0], longitude=coordinates[1], days=days, force_refresh=True)
            else:
                result = await WeatherService.get_real_weather(latitude=coordinates[0], longitude=coordinates[1], days=days)
        # Copy the cache envelope; field identity must never mutate cached data.
        if coordinate_source == 'farm' and getattr(farm, 'location_source', None) == 'region_reference':
            result = {**result, 'summary': '区域参考天气（非农场精确位置）：' + result.get('summary', '')}
        from app.services.field_environment_service import FieldEnvironmentService
        from app.core.field_environment import bind_environment_weather
        result = bind_environment_weather(result, await FieldEnvironmentService.latest(db, field))
        return {**result, "field_id": field.id, "latitude": coordinates[0] if coordinates else None,
                "longitude": coordinates[1] if coordinates else None, "coordinate_source": coordinate_source,
                "provider": config.weather_provider}

    @staticmethod
    async def test_connection(provider="open_meteo", api_key=None, custom_config=None, latitude=None, longitude=None):
        """Probe caller-supplied coordinates and actual current/48h forecast fields."""
        if finite_number(latitude, -90, 90) is None or finite_number(longitude, -180, 180) is None:
            return False, '请填写测试地点的有效经纬度；不会采用默认演示地点'
        if provider == 'custom':
            from app.services.custom_weather_service import CustomWeatherService
            result = await CustomWeatherService.fetch(custom_config or {}, api_key or '', latitude, longitude, 2, use_cache=False)
        elif provider == 'open_meteo':
            result = await WeatherService._fetch_open_meteo(latitude, longitude, 2)
        else:
            return False, '所选天气提供商尚未实现'
        if result['status'] == 'online': return True, '当前天气与未来48小时预报字段有效；这不证明来源质量或作业获准'
        return False, result.get('summary') or '天气字段缺失或接口不可用'
