"""Explicit contracts for user-configured HTTPS GET weather sources."""
import ipaddress
import re
from urllib.parse import urlsplit
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CURRENT_FIELDS = ('timestamp', 'temperature', 'humidity', 'rainfall', 'wind_speed', 'weather')
FORECAST_FIELDS = ('date', 'temp_max', 'temp_min', 'rainfall_mm', 'rain_prob', 'wind_speed', 'humidity', 'weather')


class CustomWeatherConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint: str = Field('', max_length=2048)
    forecast_endpoint: str = Field('', max_length=2048)
    source_name: str = Field('自定义天气', min_length=1, max_length=80)
    format: Literal['mapped_json', 'open_meteo'] = 'mapped_json'
    auth_type: Literal['none', 'bearer', 'header'] = 'none'
    auth_header: str = Field('X-Api-Key', max_length=64)
    location_mode: Literal['separate', 'longitude_latitude', 'latitude_longitude'] = 'separate'
    latitude_param: str = Field('latitude', min_length=1, max_length=64)
    longitude_param: str = Field('longitude', min_length=1, max_length=64)
    location_param: str = Field('location', min_length=1, max_length=64)
    days_param: str = Field('days', max_length=64)
    current_path: str = Field('current', max_length=200)
    forecast_path: str = Field('forecasts', max_length=200)
    current_fields: dict[str, str] = Field(default_factory=lambda: {name: name for name in CURRENT_FIELDS})
    forecast_fields: dict[str, str] = Field(default_factory=lambda: {name: name for name in FORECAST_FIELDS})
    temperature_unit: Literal['C', 'F', 'K'] = 'C'
    wind_unit: Literal['m/s', 'km/h', 'knots'] = 'm/s'
    precipitation_unit: Literal['mm', 'cm', 'in'] = 'mm'
    probability_unit: Literal['percent', 'fraction'] = 'percent'
    timestamp_timezone: Literal['Asia/Shanghai', 'UTC'] = 'Asia/Shanghai'

    @field_validator('endpoint', 'forecast_endpoint')
    @classmethod
    def valid_endpoint(cls, value):
        value = value.strip()
        if not value: return value
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('天气接口须为不含账号、查询参数或片段的HTTPS完整地址')
        host = parsed.hostname.lower()
        if host == 'localhost' or host.endswith('.localhost'): raise ValueError('天气接口不能为内网地址')
        try: address = ipaddress.ip_address(host)
        except ValueError: address = None
        if address is not None and not address.is_global: raise ValueError('天气接口不能为内网地址')
        try: parsed.port
        except ValueError: raise ValueError('天气接口端口无效') from None
        return value

    @field_validator('auth_header', 'latitude_param', 'longitude_param', 'location_param', 'days_param')
    @classmethod
    def valid_names(cls, value, info):
        if not value and info.field_name == 'days_param': return value
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', value): raise ValueError('参数或请求头名称无效')
        if info.field_name == 'auth_header' and value.lower() in {'host', 'authorization', 'cookie', 'content-length', 'connection', 'transfer-encoding', 'proxy-authorization'}:
            raise ValueError('请使用Bearer模式或独立API鉴权请求头')
        return value

    @field_validator('current_path', 'forecast_path')
    @classmethod
    def valid_paths(cls, value):
        if value and not re.fullmatch(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){0,7}', value): raise ValueError('字段路径只支持最多8段点分名称或数组下标')
        return value

    @field_validator('current_fields', 'forecast_fields')
    @classmethod
    def valid_mapping(cls, mapping, info):
        allowed = CURRENT_FIELDS if info.field_name == 'current_fields' else FORECAST_FIELDS
        if set(mapping) - set(allowed): raise ValueError('天气映射包含未知目标字段')
        for value in mapping.values(): cls.valid_paths(value)
        return mapping

    @model_validator(mode='after')
    def boundaries(self):
        if self.endpoint and self.forecast_endpoint:
            def origin(url):
                parsed = urlsplit(url); return parsed.scheme, parsed.hostname, parsed.port or 443
            if origin(self.endpoint) != origin(self.forecast_endpoint): raise ValueError('当前天气与预报接口必须使用同一来源域名及端口')
        names = [self.latitude_param, self.longitude_param] if self.location_mode == 'separate' else [self.location_param]
        if self.days_param: names.append(self.days_param)
        if len(names) != len(set(names)): raise ValueError('坐标与天数参数名称不能重复')
        return self


def credential_identity(provider, config):
    if provider != 'custom': return (provider,)
    values = CustomWeatherConfig.model_validate(config).model_dump()
    return (provider, values['endpoint'], values['forecast_endpoint'], values['auth_type'], values['auth_header'])
