"""Nullable, explicit weather contract shared by agricultural pages."""
from typing import Literal, Optional
from pydantic import BaseModel, Field
from app.schemas.field_environment import FieldEnvironmentResponse


class CurrentWeather(BaseModel):
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    rainfall: Optional[float] = None
    wind_speed: Optional[float] = None
    weather: str = "未知"
    source: str = "Open-Meteo 天气模型（室外）"
    timestamp: Optional[str] = None


class DailyWeather(BaseModel):
    date: str
    weather: str = "未知"
    temp_max: Optional[float] = None
    temp_min: Optional[float] = None
    rainfall_mm: Optional[float] = None
    rain_prob: Optional[float] = None
    wind_speed: Optional[float] = None
    humidity: Optional[float] = None


class SpraySafety(BaseModel):
    assessment_scope: Optional[str] = None
    has_rain_soon: bool = False
    high_temp_risk: bool = False
    strong_wind_risk: bool = False
    is_offline_warning: bool = True
    spray_risks: list[str] = Field(default_factory=list)
    is_suitable_to_spray: bool = False
    policy_version: str = "precaution-v1"


class FieldWeatherResponse(BaseModel):
    environment: Optional[FieldEnvironmentResponse] = None
    outdoor_weather_assessment: Optional[SpraySafety] = None
    field_id: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    coordinate_source: Optional[str] = None
    status: Literal["online", "partial", "offline", "location_missing", "unsupported_provider"] = "offline"
    is_fallback: bool = True
    provider: str = "open_meteo"
    source: str = "Open-Meteo 天气模型（室外）"
    fetched_at: Optional[str] = None
    summary: str = "暂无有效天气数据"
    current: CurrentWeather = Field(default_factory=CurrentWeather)
    forecasts: list[DailyWeather] = Field(default_factory=list)
    spray_safety: SpraySafety = Field(default_factory=SpraySafety)
    units: dict[str, str] = Field(default_factory=lambda: {"temperature": "℃", "humidity": "%", "wind_speed": "m/s", "rainfall": "mm"})
