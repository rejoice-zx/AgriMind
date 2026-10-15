"""Exercise actual parser, DB-backed configuration and plan confirmation contracts."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
import pytest_asyncio
from fastapi import HTTPException, FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, func
from app.core.weather_policy import agriculture_today, AGRI_TIMEZONE, evaluate_spray_weather
from app.services.weather_service import WeatherService, _WEATHER_CACHE
from app.services.agri_plan_service import AgriPlanService
from app.services.settings_service import SettingsService, AgriAIConfig
from app.services.agent_service import AgriTools, AgriAgentService
from app.services.vision_service import VisionService
from app.schemas.agriculture import AgriPlanRequest
from app.schemas.agent import AgentDecisionRequest
from app.models.farm import Farm
from app.models.field import Field
from app.models.crop import CropPlanting
from app.models.farming_record import FarmingRecord
from app.models.user import User
from app.database import get_db
from app.api.dependencies import get_current_user


def provider_data(days=14, rain=0, probability=0, temperature=24, wind=3.6, unit="km/h"):
    return {"current_units": {"temperature_2m": "°C", "relative_humidity_2m": "%", "precipitation": "mm", "wind_speed_10m": unit},
        "daily_units": {"temperature_2m_max": "°C", "temperature_2m_min": "°C", "precipitation_sum": "mm", "precipitation_probability_max": "%", "wind_speed_10m_max": unit},
        "current": {"time": datetime.now(AGRI_TIMEZONE).isoformat(), "temperature_2m": temperature,
            "relative_humidity_2m": 70, "precipitation": rain, "wind_speed_10m": wind, "weather_code": 3},
        "daily": {"time": [(agriculture_today()+timedelta(days=i)).isoformat() for i in range(days)],
            "temperature_2m_max": [temperature]*days, "temperature_2m_min": [18]*days,
            "precipitation_sum": [rain]*days, "precipitation_probability_max": [probability]*days,
            "wind_speed_10m_max": [wind]*days, "weather_code": [3]*days}}


def test_parser_converts_wind_units_and_does_not_invent_humidity():
    result = WeatherService._parse_open_meteo_response(provider_data(), 14)
    assert result["current"]["wind_speed"] == 1
    assert len(result["forecasts"]) == 14
    assert result["forecasts"][1]["humidity"] is None
    assert result["spray_safety"]["is_suitable_to_spray"]


@pytest.mark.parametrize("changes,reason", [({"rain": 12}, "降水量"), ({"probability": 90}, "降雨概率"), ({"temperature": 40}, "最高温"), ({"wind": 14.4}, "风速")])
def test_independent_weather_risks(changes, reason):
    weather = WeatherService._parse_open_meteo_response(provider_data(**changes), 7)
    assert not weather["spray_safety"]["is_suitable_to_spray"]
    assert any(reason in risk for risk in weather["spray_safety"]["spray_risks"])


def test_missing_units_dates_and_nulls_are_unknown():
    data = provider_data(days=1)
    data["daily_units"] = {}
    parsed = WeatherService._parse_open_meteo_response(data, 14)
    assert parsed["status"] == "partial"
    assert not parsed["spray_safety"]["is_suitable_to_spray"]
    assert evaluate_spray_weather(parsed["forecasts"][0])["reason_code"] == "weather_unknown"
    assert evaluate_spray_weather({})["can_proceed"] is False


def test_stale_current_weather_is_not_approval_evidence():
    data = provider_data()
    data["current"]["time"] = (datetime.now(AGRI_TIMEZONE) - timedelta(hours=3)).isoformat()
    weather = WeatherService._parse_open_meteo_response(data, 7)
    assert weather["current"]["temperature"] is None
    assert weather["status"] == "partial"
    assert not weather["spray_safety"]["is_suitable_to_spray"]
    assert "过期" in weather["summary"]


@pytest_asyncio.fixture
async def field_context(test_session):
    user = User(username="stage-a-grower", password_hash="not-a-login", role="customer", is_active=True)
    test_session.add(user); await test_session.flush()
    farm = Farm(name="Actual farm", owner_id=user.id, latitude=0, longitude=0)
    test_session.add(farm); await test_session.flush()
    field = Field(name="Synthetic outdoor field", farm_id=farm.id, area=1, facility_type="open_field")
    test_session.add(field); await test_session.flush()
    crop = CropPlanting(field_id=field.id, crop="番茄", planting_date=agriculture_today(), growth_stage="开花期", status="active")
    test_session.add(crop); await test_session.commit()
    return user, farm, field, crop


@pytest.fixture
def actual_weather(monkeypatch):
    async def configure(**kwargs):
        return WeatherService._parse_open_meteo_response(provider_data(), kwargs["days"])
    monkeypatch.setattr(WeatherService, "get_real_weather", configure)


@pytest.mark.asyncio
async def test_coordinate_zero_and_missing_location(test_session, field_context, monkeypatch):
    user, farm, field, _ = field_context
    calls = []
    async def fetch(**kwargs):
        calls.append(kwargs); return WeatherService._parse_open_meteo_response(provider_data(), kwargs["days"])
    monkeypatch.setattr(WeatherService, "get_real_weather", fetch)
    result = await WeatherService.get_field_weather(test_session, field)
    assert calls[0]["latitude"] == calls[0]["longitude"] == 0
    assert result["coordinate_source"] == "farm"
    farm.latitude = farm.longitude = None; await test_session.commit()
    result = await WeatherService.get_field_weather(test_session, field)
    assert result["status"] == "location_missing"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_real_weather_fields_drive_plan_and_offline_returns_a_plan(test_session, field_context, monkeypatch):
    user, _, field, _ = field_context
    async def rainy(**kwargs): return WeatherService._parse_open_meteo_response(provider_data(rain=12, probability=90), kwargs["days"])
    monkeypatch.setattr(WeatherService, "get_real_weather", rainy)
    plan = await AgriPlanService.generate_plan(test_session, AgriPlanRequest(field_id=field.id), user.id, user.role)
    assert all(item.can_proceed == (not item.is_spray) for item in plan.items)
    assert "12mm" in plan.items[0].reason and "90%" in plan.items[0].reason
    async def offline(**kwargs): return WeatherService._fallback_safe_weather(kwargs["days"])
    monkeypatch.setattr(WeatherService, "get_real_weather", offline)
    plan = await AgriPlanService.generate_plan(test_session, AgriPlanRequest(field_id=field.id, force_refresh=True), user.id, user.role)
    assert len(plan.items) == 7 and all(item.reason_code == "weather_unknown" for item in plan.items)


@pytest.mark.asyncio
async def test_missing_forecast_days_cannot_be_assumed_clear(test_session, field_context, monkeypatch):
    user, _, field, _ = field_context
    async def short(**kwargs): return WeatherService._parse_open_meteo_response(provider_data(days=7), kwargs["days"])
    monkeypatch.setattr(WeatherService, "get_real_weather", short)
    plan = await AgriPlanService.generate_plan(test_session, AgriPlanRequest(field_id=field.id, days=14), user.id, user.role)
    assert all(item.can_proceed == (not item.is_spray) for item in plan.items[7:])
    assert all(item.reason_code == 'weather_unknown' for item in plan.items[7:])


@pytest.mark.asyncio
async def test_confirm_revalidates_proof_day_and_keeps_offline_evidence_idempotently(test_session, field_context, actual_weather, monkeypatch):
    user, _, field, _ = field_context
    plan = await AgriPlanService.generate_plan(test_session, AgriPlanRequest(field_id=field.id), user.id, user.role)
    item = plan.items[0]
    for invalid in (item.model_copy(update={"action": "forged action"}), plan.items[1], plan.items[2]):
        with pytest.raises(HTTPException) as rejected:
            await AgriPlanService.apply_plan_items(test_session, field.id, [invalid], "test", user.id, user.role)
        assert rejected.value.status_code == 409
    async def offline(**kwargs): return WeatherService._fallback_safe_weather(kwargs["days"])
    original = WeatherService.get_real_weather
    monkeypatch.setattr(WeatherService, "get_real_weather", offline)
    first = await AgriPlanService.apply_plan_items(test_session, field.id, [item], "test", user.id, user.role)
    assert await test_session.scalar(select(func.count(FarmingRecord.id))) == 1
    monkeypatch.setattr(WeatherService, "get_real_weather", original)
    second = await AgriPlanService.apply_plan_items(test_session, field.id, [item], "test", user.id, user.role)
    assert first.record_ids == second.record_ids and await test_session.scalar(select(func.count(FarmingRecord.id))) == 1
    assert first.created_count == 1 and second.created_count == 0
    record = await test_session.get(FarmingRecord, first.record_ids[0])
    assert record.safety_interval_days is None and record.input_amount is None


@pytest.mark.asyncio
async def test_saved_models_are_used_by_agent_and_vision(test_session, field_context, actual_weather, monkeypatch):
    user, _, field, _ = field_context
    config = AgriAIConfig(llm_api_key="test-only-decision", llm_base_url="https://decision.example.test/v1", llm_model="chosen-decision",
        vision_api_key="test-only-vision", vision_base_url="https://vision.example.test/v1", vision_model="chosen-vision")
    await SettingsService.set_agri_ai_config(test_session, config)
    calls = {}
    async def synth(**kwargs):
        calls["decision"] = kwargs
        return {"conclusion": "test", "can_proceed": False, "confidence": .8, "recommendations": [], "risks": []}
    async def vision(**kwargs): calls["vision"] = kwargs; return {"need_expert": True}
    async def tools(cls, name, arguments, db):
        from app.schemas.agent import ToolExecuteResponse
        return ToolExecuteResponse(tool_name=name, status="error", result=None, error="controlled unavailable")
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", synth)
    from app.services.agent_service import AgriToolRegistry
    monkeypatch.setattr(AgriToolRegistry, "execute_tool", classmethod(tools))
    monkeypatch.setattr(VisionService, "diagnose_leaf_image", vision)
    await AgriAgentService.decide(test_session, AgentDecisionRequest(query="种植咨询"), current_user=user)
    await AgriTools.diagnose_crop_image("/static/uploads/diagnosis/test.png", db=test_session)
    assert calls["decision"]["api_key"] == config.llm_api_key
    assert calls["decision"]["base_url"] == config.llm_base_url
    assert calls["decision"]["model_name"] == config.llm_model
    assert calls["vision"]["api_key"] == config.vision_api_key
    assert calls["vision"]["base_url"] == config.vision_base_url
    assert calls["vision"]["model_name"] == config.vision_model
    changed = await SettingsService.resolve_agri_model(test_session, "vision", base_url="https://other.example.test/v1")
    assert changed["api_key"] == ""


@pytest.mark.asyncio
async def test_provider_url_requests_explicit_units_and_days(monkeypatch):
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url):
            calls.append(url); return SimpleNamespace(status_code=200, json=lambda: provider_data())
    monkeypatch.setattr("app.services.weather_service.httpx.AsyncClient", Client)
    _WEATHER_CACHE.clear()
    await WeatherService.get_real_weather(0, 0, 14)
    assert "wind_speed_unit=ms" in calls[0] and "forecast_days=14" in calls[0]


@pytest.mark.asyncio
async def test_weather_api_authorizes_before_fetch_and_serializes_contract(test_session, field_context, monkeypatch):
    from app.api.farming_records import router
    user, _, field, _ = field_context
    caller = {"user": user}
    calls = []
    async def fetch(**kwargs):
        calls.append(kwargs)
        return WeatherService._parse_open_meteo_response(provider_data(), kwargs["days"])
    monkeypatch.setattr(WeatherService, "get_real_weather", fetch)
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: caller["user"]
    async def session(): yield test_session
    app.dependency_overrides[get_db] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/fields/{field.id}/weather/forecast?days=14")
        assert response.status_code == 200
        assert response.json()["current"]["source"].endswith("（室外）")
        assert len(response.json()["forecasts"]) == 14
        caller["user"] = SimpleNamespace(id=user.id + 100, role="customer", is_active=True)
        denied = await client.get(f"/api/fields/{field.id}/weather/forecast")
        assert denied.status_code == 403 and len(calls) == 1


@pytest.mark.asyncio
async def test_upload_returned_url_is_served_as_image(field_context, test_session):
    """HTTP/static path plumbing only; this artificial fixture is not a disease diagnosis."""
    import struct
    from pathlib import Path
    from app.config import settings
    from app.api.expert_workbench import router
    user, _, _, _ = field_context
    upload_root = Path(settings.DATA_DIR) / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(); app.include_router(router)
    async def isolated_db(): yield test_session
    app.dependency_overrides[get_db] = isolated_db
    app.dependency_overrides[get_current_user] = lambda: user
    image = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 400, 400) + b"\x08\x02\x00\x00\x00" + b"fixture"*400
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        uploaded = await client.post("/api/diagnosis/upload", files={"file": ("fixture.png", image, "image/png")})
        assert uploaded.status_code == 200
        served = await client.get(uploaded.json()["url"])
        assert served.status_code == 200 and served.content == image
        assert served.headers["content-type"].startswith("image/")


@pytest.mark.asyncio
async def test_missing_gdd_and_weather_do_not_replace_banned_substance_block(test_session, field_context, monkeypatch):
    user, _, _, _ = field_context
    from app.schemas.agent import ToolExecuteResponse
    from app.services.agent_service import AgriToolRegistry
    async def tool(name, arguments, db):
        return ToolExecuteResponse(tool_name=name, status="error", result=None, error="no evidence")
    async def model(**kwargs):
        return {"can_proceed": True, "confidence": .99, "conclusion": "controlled model approval", "recommendations": [], "risks": []}
    monkeypatch.setattr(AgriToolRegistry, "execute_tool", tool)
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", model)
    decision = await AgriAgentService.decide(test_session, AgentDecisionRequest(query="积温阶段可以用药克百威吗"), current_user=user)
    assert not decision.can_proceed and decision.need_expert
    assert "法规硬阻断" in decision.conclusion
