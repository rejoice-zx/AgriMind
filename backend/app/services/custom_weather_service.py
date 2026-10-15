"""Bounded HTTPS weather calls and explicit JSON mapping; no invented observations."""
import asyncio
from collections import OrderedDict
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time

import httpx
from app.schemas.custom_weather import CustomWeatherConfig
from app.core.weather_policy import AGRI_TIMEZONE, finite_number, agriculture_today
from app.services.weather_requests import share_weather_request


class WeatherSourceError(Exception):
    pass


class CustomWeatherService:
    MAX_BYTES = 262144
    TOTAL_SECONDS = 10
    _cache = OrderedDict()

    @staticmethod
    def create_client():
        return httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False,
                                 headers={'Accept-Encoding': 'identity', 'Accept': 'application/json'})

    @staticmethod
    async def validate_address(url):
        from app.utils.security import validate_safe_external_url
        safe, _ = await asyncio.to_thread(validate_safe_external_url, url)
        if not safe: raise WeatherSourceError('自定义天气地址未通过外部HTTPS地址检查')

    @classmethod
    async def request(cls, client, url, params, headers):
        await cls.validate_address(url)
        async with client.stream('GET', url, params=params, headers=headers) as response:
            if response.status_code != 200: raise WeatherSourceError('自定义天气接口HTTP状态：' + str(response.status_code))
            chunks = []; count = 0
            async for chunk in response.aiter_raw():
                count += len(chunk)
                if count > cls.MAX_BYTES: raise WeatherSourceError('天气接口响应超过256KiB限制')
                chunks.append(chunk)
            try: data = json.loads(b''.join(chunks))
            except (ValueError, UnicodeError): raise WeatherSourceError('天气接口返回无效JSON') from None
            if not isinstance(data, dict): raise WeatherSourceError('天气响应必须为JSON对象')
            return data

    @staticmethod
    def lookup(value, path):
        if not path: return None
        for key in path.split('.'):
            if isinstance(value, dict): value = value.get(key)
            elif isinstance(value, list) and key.isdigit() and int(key) < len(value): value = value[int(key)]
            else: return None
        return value

    @staticmethod
    def number(value):
        if isinstance(value, str):
            try: value = float(value)
            except (ValueError, OverflowError): return None
        try: return finite_number(value)
        except OverflowError: return None

    @classmethod
    def normalize(cls, current_body, forecast_body, config, days):
        if config.format == 'open_meteo':
            return {**current_body, 'daily': forecast_body.get('daily'), 'daily_units': forecast_body.get('daily_units')}
        current = cls.lookup(current_body, config.current_path) if config.current_path else current_body
        daily = cls.lookup(forecast_body, config.forecast_path) if config.forecast_path else forecast_body
        if current is None: current = {}
        if daily is None: daily = []
        if not isinstance(current, dict) or not isinstance(daily, list): raise WeatherSourceError('天气JSON路径未指向当前天气对象与预报数组')
        def mapped(data, mappings, name): return cls.lookup(data, mappings.get(name, ''))
        def temperature(value):
            value = cls.number(value)
            if value is None: return None
            return (value - 32) * 5 / 9 if config.temperature_unit == 'F' else value - 273.15 if config.temperature_unit == 'K' else value
        def wind(value):
            value = cls.number(value)
            return None if value is None else value / 3.6 if config.wind_unit == 'km/h' else value * 0.514444 if config.wind_unit == 'knots' else value
        def rain(value):
            value = cls.number(value)
            return None if value is None else value * 10 if config.precipitation_unit == 'cm' else value * 25.4 if config.precipitation_unit == 'in' else value
        observed = mapped(current, config.current_fields, 'timestamp')
        try:
            if not isinstance(observed, str) or not ('T' in observed or ' ' in observed): raise ValueError()
            timestamp = datetime.fromisoformat(observed.replace('Z', '+00:00'))
            if timestamp.tzinfo is None: timestamp = timestamp.replace(tzinfo=timezone.utc if config.timestamp_timezone == 'UTC' else AGRI_TIMEZONE)
            observed = timestamp.isoformat()
        except (ValueError, TypeError): observed = None
        forecasts = {'time': [], 'temperature_2m_max': [], 'temperature_2m_min': [], 'precipitation_sum': [],
                     'precipitation_probability_max': [], 'wind_speed_10m_max': []}
        seen = set()
        for row in daily[:days]:
            if not isinstance(row, dict): raise WeatherSourceError('预报数组存在无效条目')
            day = mapped(row, config.forecast_fields, 'date')
            if isinstance(day, str) and day in seen: raise WeatherSourceError('预报日期重复，不能确定应采用哪条数据')
            if isinstance(day, str): seen.add(day)
            forecasts['time'].append(day)
            forecasts['temperature_2m_max'].append(temperature(mapped(row, config.forecast_fields, 'temp_max')))
            forecasts['temperature_2m_min'].append(temperature(mapped(row, config.forecast_fields, 'temp_min')))
            forecasts['precipitation_sum'].append(rain(mapped(row, config.forecast_fields, 'rainfall_mm')))
            probability = cls.number(mapped(row, config.forecast_fields, 'rain_prob'))
            forecasts['precipitation_probability_max'].append(None if probability is None else probability * 100 if config.probability_unit == 'fraction' else probability)
            forecasts['wind_speed_10m_max'].append(wind(mapped(row, config.forecast_fields, 'wind_speed')))
        return {'current': {'time': observed, 'temperature_2m': temperature(mapped(current, config.current_fields, 'temperature')),
                           'relative_humidity_2m': cls.number(mapped(current, config.current_fields, 'humidity')),
                           'precipitation': rain(mapped(current, config.current_fields, 'rainfall')),
                           'wind_speed_10m': wind(mapped(current, config.current_fields, 'wind_speed'))},
                'current_units': {'temperature_2m': '°C', 'relative_humidity_2m': '%', 'precipitation': 'mm', 'wind_speed_10m': 'm/s'},
                'daily': forecasts, 'daily_units': {'temperature_2m_max': '°C', 'temperature_2m_min': '°C',
                    'precipitation_sum': 'mm', 'precipitation_probability_max': '%', 'wind_speed_10m_max': 'm/s'}}

    @classmethod
    async def fetch(cls, values, api_key, latitude, longitude, days=7, use_cache=True, force_refresh=False):
        if not use_cache:
            return await cls._fetch(values, api_key, latitude, longitude, days, use_cache=False)
        try:
            config = CustomWeatherConfig.model_validate(values)
            # Credentials participate in isolation but are never stored in task keys.
            identity = json.dumps([config.model_dump(), api_key, latitude, longitude,
                max(1, min(days, 14)), agriculture_today().isoformat()], sort_keys=True)
            key = ('custom_weather', hashlib.sha256(identity.encode()).digest(), force_refresh)
        except (ValueError, TypeError):
            return await cls._fetch(values, api_key, latitude, longitude, days)
        return await share_weather_request(key, lambda: cls._fetch(values, api_key, latitude, longitude, days, force_refresh=force_refresh))

    @classmethod
    async def _fetch(cls, values, api_key, latitude, longitude, days=7, use_cache=True, force_refresh=False):
        from app.services.weather_service import WeatherService
        days = max(1, min(days, 14))
        if finite_number(latitude, -90, 90) is None or finite_number(longitude, -180, 180) is None:
            return WeatherService._fallback_safe_weather(days, 'location_missing', '未填写有效天气查询坐标')
        try:
            config = CustomWeatherConfig.model_validate(values)
            if not config.endpoint: raise WeatherSourceError('请填写自定义天气完整HTTPS接口地址')
            if config.auth_type != 'none' and not api_key: raise WeatherSourceError('此天气地址没有可用鉴权密钥')
            if '\r' in api_key or '\n' in api_key: raise WeatherSourceError('天气鉴权信息格式无效')
            from app.config import settings
            signing_key = settings.SETTINGS_ENCRYPTION_KEY or settings.JWT_SECRET_KEY
            private_scope = hmac.new(signing_key.encode(), api_key.encode(), hashlib.sha256).hexdigest()
            cache_key = (json.dumps(config.model_dump(), sort_keys=True), private_scope, latitude, longitude, days, agriculture_today().isoformat())
            cached = cls._cache.get(cache_key)
            if use_cache and not force_refresh and cached and cached[0] > time.monotonic():
                cls._cache.move_to_end(cache_key); return copy.deepcopy(cached[1])
            params = {}
            if config.format == 'open_meteo':
                params = {'latitude': latitude, 'longitude': longitude, 'forecast_days': days, 'timezone': 'Asia/Shanghai',
                    'wind_speed_unit': 'ms', 'current': 'temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code',
                    'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max'}
            else:
                if config.location_mode == 'separate': params.update({config.latitude_param: latitude, config.longitude_param: longitude})
                else: params[config.location_param] = f'{longitude},{latitude}' if config.location_mode == 'longitude_latitude' else f'{latitude},{longitude}'
                if config.days_param: params[config.days_param] = days
            headers = {}
            if config.auth_type == 'bearer': headers['Authorization'] = 'Bearer ' + api_key
            elif config.auth_type == 'header': headers[config.auth_header] = api_key
            async with asyncio.timeout(cls.TOTAL_SECONDS):
                async with cls.create_client() as client:
                    current = await cls.request(client, config.endpoint, params, headers)
                    forecast = await cls.request(client, config.forecast_endpoint, params, headers) if config.forecast_endpoint and config.forecast_endpoint != config.endpoint else current
            result = WeatherService._parse_open_meteo_response(cls.normalize(current, forecast, config, days), days)
            source = config.source_name + '（用户配置来源）'
            result['source'] = source; result['current']['source'] = source
            if config.format == 'mapped_json':
                current_data = cls.lookup(current, config.current_path) if config.current_path else current
                description = cls.lookup(current_data, config.current_fields.get('weather', ''))
                if isinstance(description, str) and result['current']['temperature'] is not None:
                    result['current']['weather'] = description[:80]
                daily_data = cls.lookup(forecast, config.forecast_path) if config.forecast_path else forecast
                by_date = {cls.lookup(row, config.forecast_fields.get('date', '')): row for row in (daily_data or [])[:days] if isinstance(row, dict) and isinstance(cls.lookup(row, config.forecast_fields.get('date', '')), str)}
                for item in result['forecasts']:
                    row = by_date.get(item['date'], {})
                    description = cls.lookup(row, config.forecast_fields.get('weather', ''))
                    if isinstance(description, str): item['weather'] = description[:80]
                    item['humidity'] = finite_number(cls.number(cls.lookup(row, config.forecast_fields.get('humidity', ''))), 0, 100)
            if use_cache:
                ttl = 600 if result['status'] == 'online' else 60
                observed = result['current'].get('timestamp')
                if observed and result['current']['temperature'] is not None:
                    remaining = 7200 - (datetime.now(AGRI_TIMEZONE) - datetime.fromisoformat(observed)).total_seconds()
                    ttl = min(ttl, max(0, remaining))
                cls._cache[cache_key] = (time.monotonic() + ttl, copy.deepcopy(result))
                cls._cache.move_to_end(cache_key)
                while len(cls._cache) > 128: cls._cache.popitem(last=False)
            return result
        except asyncio.CancelledError: raise
        except WeatherSourceError as error: reason = str(error)
        except TimeoutError: reason = '自定义天气调用超过10秒总时限'
        except Exception: reason = '自定义天气配置、响应或网络不可用，请核查接口契约'
        return WeatherService._fallback_safe_weather(days, 'offline', reason)
