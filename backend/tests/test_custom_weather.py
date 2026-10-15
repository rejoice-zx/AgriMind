"""Controlled HTTPS/ASGI tests for custom weather; never use an external account."""
import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from app.core.weather_policy import AGRI_TIMEZONE, agriculture_today
from app.schemas.custom_weather import CustomWeatherConfig
from app.services.custom_weather_service import CustomWeatherService
from app.services.weather_service import WeatherService
from app.services.settings_service import SettingsService, AgriAIConfig
from app.database import get_db
from app.api.dependencies import get_current_user
from app.api.settings import router as settings_router
from app.models.user import User
from app.models.farm import Farm
from app.models.field import Field


def weather_body():
    today = agriculture_today()
    return {'current': {'timestamp': datetime.now(AGRI_TIMEZONE).isoformat(), 'temperature': 20, 'humidity': 50,
                        'rainfall': 0, 'wind_speed': 1, 'weather': '阴'},
            'forecasts': [{'date': (today + timedelta(days=i)).isoformat(), 'temp_max': 25, 'temp_min': 15,
                          'rainfall_mm': 0, 'rain_prob': 0, 'wind_speed': 1, 'humidity': 60, 'weather': '多云'} for i in range(2)]}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data): self.data = data
    async def __aiter__(self):
        yield self.data[:10]
        yield self.data[10:]


@pytest.fixture
def source(monkeypatch, controlled_dns):
    controlled_dns({'weather.example': ['93.184.216.34'], 'other.example': ['93.184.216.35'],
                    'api.siliconflow.cn': ['93.184.216.34']})
    state = SimpleNamespace(body=weather_body(), calls=[], status=200, slow=False, raw=None)
    real_client = httpx.AsyncClient
    async def handler(request):
        state.calls.append(request)
        if state.slow: await asyncio.Event().wait()
        data = state.raw if state.raw is not None else json.dumps(state.body).encode()
        return httpx.Response(state.status, stream=Chunks(data), headers={'Location': 'https://other.example/target'} if state.status == 307 else {})
    monkeypatch.setattr(CustomWeatherService, 'create_client', staticmethod(lambda: real_client(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False)))
    monkeypatch.setattr(CustomWeatherService, '_cache', __import__('collections').OrderedDict())
    return state


def config(**changes): return CustomWeatherConfig(endpoint='https://weather.example/current', **changes).model_dump()


@pytest.mark.asyncio
async def test_concurrent_custom_weather_shares_transport_but_isolates_credentials_and_test_calls(source, monkeypatch):
    original = CustomWeatherService.request
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        entered.set(); await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(CustomWeatherService, 'request', delayed)
    cfg = config(auth_type='bearer')
    first = asyncio.create_task(CustomWeatherService.fetch(cfg, 'synthetic-A', 1, 2, 2))
    await entered.wait()
    second = asyncio.create_task(CustomWeatherService.fetch(cfg, 'synthetic-A', 1, 2, 2))
    other = asyncio.create_task(CustomWeatherService.fetch(cfg, 'synthetic-B', 1, 2, 2))
    await asyncio.sleep(0); release.set()
    results = await asyncio.gather(first, second, other)
    assert len(source.calls) == 2
    results[0]['current']['temperature'] = 99
    assert results[1]['current']['temperature'] == 20
    await CustomWeatherService.fetch(cfg, 'synthetic-A', 1, 2, 2, use_cache=False)
    assert len(source.calls) == 3


@pytest.mark.asyncio
async def test_mapped_current_and_forecasts_use_real_zero_fields_and_source(source):
    result = await CustomWeatherService.fetch(config(), '', 0, 0, 2)
    assert result['status'] == 'online' and result['current']['rainfall'] == 0
    assert result['current']['weather'] == '阴' and result['forecasts'][0]['weather'] == '多云'
    assert result['forecasts'][0]['humidity'] == 60
    assert result['source'] == '自定义天气（用户配置来源）'
    assert result['spray_safety']['is_suitable_to_spray']
    request = source.calls[0]
    assert request.url.params['latitude'] == '0' and request.url.params['longitude'] == '0'
    assert 'authorization' not in request.headers


@pytest.mark.asyncio
async def test_nested_mapping_units_and_risky_weather_do_not_become_approval(source):
    body = weather_body(); body['current']['temperature'] = '68'; body['current']['wind_speed'] = '3.6'
    body['forecasts'][0].update(temp_max='77', temp_min='59', rainfall_mm='0.4', rain_prob='0.75', wind_speed='18')
    body['forecasts'][1].update(temp_max=77, temp_min=59, wind_speed=3.6)
    source.body = {'payload': {'now': body['current'], 'daily': body['forecasts']}}
    cfg = config(current_path='payload.now', forecast_path='payload.daily', temperature_unit='F',
                 wind_unit='km/h', precipitation_unit='cm', probability_unit='fraction')
    result = await CustomWeatherService.fetch(cfg, '', 1, 2, 2)
    assert result['current']['temperature'] == 20 and result['current']['wind_speed'] == 1
    assert result['forecasts'][0]['rainfall_mm'] == 4 and result['forecasts'][0]['rain_prob'] == 75
    assert result['status'] == 'online' and not result['spray_safety']['is_suitable_to_spray']


@pytest.mark.asyncio
async def test_missing_or_stale_measurements_are_unknown_not_zero(source):
    source.body['current']['timestamp'] = (datetime.now(AGRI_TIMEZONE) - timedelta(hours=3)).isoformat()
    del source.body['forecasts'][0]['rain_prob']
    source.body['forecasts'][1]['wind_speed'] = True
    result = await CustomWeatherService.fetch(config(), '', 1, 2, 2)
    assert result['status'] == 'partial' and result['current']['temperature'] is None
    assert result['forecasts'][0]['rain_prob'] is None and result['forecasts'][1]['wind_speed'] is None
    assert not result['spray_safety']['is_suitable_to_spray']


@pytest.mark.asyncio
async def test_two_endpoints_and_combined_coordinates_share_only_explicit_header(source):
    cfg = config(forecast_endpoint='https://weather.example/daily', auth_type='header', auth_header='X-Weather-Key',
                 location_mode='longitude_latitude', location_param='location', days_param='')
    result = await CustomWeatherService.fetch(cfg, 'synthetic-key', 1, 2, 2)
    assert result['status'] == 'online' and len(source.calls) == 2
    assert all(request.url.params['location'] == '2,1' for request in source.calls)
    assert all(request.headers['X-Weather-Key'] == 'synthetic-key' and 'synthetic-key' not in str(request.url) for request in source.calls)
    assert 'synthetic-key' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 429, 500, 307])
async def test_errors_and_redirects_do_not_leak_body_or_repeat_calls(source, status):
    source.status = status; source.body = {'message': 'synthetic-private-upstream-error'}
    result = await CustomWeatherService.fetch(config(auth_type='bearer'), 'synthetic-key', 1, 2, 2)
    assert result['is_fallback'] and not result['spray_safety']['is_suitable_to_spray']
    assert len(source.calls) == 1 and 'synthetic-private-upstream-error' not in json.dumps(result)


@pytest.mark.asyncio
async def test_body_limit_bad_json_duplicates_and_timeout(source, monkeypatch):
    monkeypatch.setattr(CustomWeatherService, 'MAX_BYTES', 32)
    result = await CustomWeatherService.fetch(config(), '', 1, 2, 2)
    assert result['is_fallback'] and '256KiB' in result['summary']
    monkeypatch.setattr(CustomWeatherService, 'MAX_BYTES', 262144)
    source.raw = b'not-json'
    assert (await CustomWeatherService.fetch(config(), '', 1, 2, 2))['is_fallback']
    source.raw = None; source.body['forecasts'][1]['date'] = source.body['forecasts'][0]['date']
    assert (await CustomWeatherService.fetch(config(), '', 1, 2, 2))['is_fallback']
    source.slow = True; monkeypatch.setattr(CustomWeatherService, 'TOTAL_SECONDS', 0.2)
    assert '10秒' in (await CustomWeatherService.fetch(config(), '', 1, 2, 2))['summary']


@pytest.mark.asyncio
async def test_cache_is_scoped_bounded_copied_and_does_not_reuse_authentication(source):
    cfg = config(auth_type='bearer')
    first = await CustomWeatherService.fetch(cfg, 'synthetic-A', 1, 2, 2)
    first['current']['temperature'] = 99
    again = await CustomWeatherService.fetch(cfg, 'synthetic-A', 1, 2, 2)
    assert len(source.calls) == 1 and again['current']['temperature'] == 20
    await CustomWeatherService.fetch(cfg, 'synthetic-B', 1, 2, 2)
    assert len(source.calls) == 2 and source.calls[-1].headers['Authorization'] == 'Bearer synthetic-B'
    assert 'synthetic-A' not in repr(tuple(CustomWeatherService._cache))
    for index in range(130): await CustomWeatherService.fetch(cfg, 'synthetic-A', 1, index / 10, 2)
    assert len(CustomWeatherService._cache) == 128


@pytest.mark.parametrize('changes', [
    {'endpoint': 'http://weather.example/current'}, {'endpoint': 'https://127.0.0.1/current'},
    {'endpoint': 'https://name:password@weather.example/current'}, {'endpoint': 'https://weather.example/current?key=synthetic'},
    {'forecast_endpoint': 'https://other.example/daily'}, {'auth_header': 'Host'},
    {'current_path': 'data[0]'}, {'current_fields': {'unknown': 'value'}}, {'longitude_param': 'latitude'},
])
def test_configuration_rejects_unsafe_or_ambiguous_contracts(changes):
    values = config(); values.update(changes)
    with pytest.raises(ValidationError): CustomWeatherConfig.model_validate(values)


@pytest.mark.asyncio
async def test_private_dns_and_missing_key_never_enter_http_transport(source, controlled_dns):
    controlled_dns({'weather.example': ['127.0.0.1']})
    assert (await CustomWeatherService.fetch(config(), '', 1, 2, 2))['is_fallback']
    assert (await CustomWeatherService.fetch(config(auth_type='bearer'), '', 1, 2, 2))['is_fallback']
    assert not source.calls


@pytest.mark.asyncio
async def test_saved_configuration_drives_field_lookup_and_test_draft_does_not_save(test_session, source):
    owner = User(username='weather-owner', password_hash='unused', role='customer', is_active=True)
    test_session.add(owner); await test_session.flush()
    farm = Farm(name='weather-farm', owner_id=owner.id, latitude=3, longitude=4)
    test_session.add(farm); await test_session.flush()
    field = Field(name='weather-field', farm_id=farm.id, latitude=1, longitude=2)
    test_session.add(field); await test_session.commit()
    await SettingsService.set_agri_ai_config(test_session, AgriAIConfig(weather_provider='custom', weather_custom=config(), weather_api_key=''))
    result = await WeatherService.get_field_weather(test_session, field, 2)
    assert result['provider'] == 'custom' and result['coordinate_source'] == 'field'
    assert float(source.calls[-1].url.params['latitude']) == 1
    app = FastAPI(); app.include_router(settings_router)
    async def db(): yield test_session
    async def admin(): return User(id=999, username='test-admin', password_hash='unused', role='admin', is_active=True)
    app.dependency_overrides[get_db] = db; app.dependency_overrides[get_current_user] = admin
    before = await SettingsService.get_setting(test_session, SettingsService.KEY_AGRI_AI_CONFIG)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.post('/api/settings/test-weather-connection', json={'provider': 'custom', 'latitude': 0, 'longitude': 0, 'weather_custom': config()})
    assert response.status_code == 200 and response.json()['success']
    assert await SettingsService.get_setting(test_session, SettingsService.KEY_AGRI_AI_CONFIG) == before
    from app.api.farming_records import router as weather_router
    app.include_router(weather_router)
    denied_calls = []
    original_fetch = CustomWeatherService.fetch
    async def forbidden(*args, **kwargs):
        denied_calls.append(True)
        return await original_fetch(*args, **kwargs)
    from unittest.mock import patch
    async def stranger(): return User(id=888, username='other-owner', password_hash='unused', role='customer', is_active=True)
    app.dependency_overrides[get_current_user] = stranger
    with patch.object(CustomWeatherService, 'fetch', forbidden):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            response = await client.get(f'/api/fields/{field.id}/weather/forecast?days=2')
    assert response.status_code == 403 and not denied_calls


@pytest.mark.asyncio
async def test_saved_weather_key_is_masked_and_not_forwarded_after_address_change(test_session, source):
    app = FastAPI(); app.include_router(settings_router)
    async def db(): yield test_session
    identity = User(id=999, username='test-admin', password_hash='unused', role='admin', is_active=True)
    async def admin(): return identity
    app.dependency_overrides[get_db] = db; app.dependency_overrides[get_current_user] = admin
    saved = config(auth_type='bearer')
    await SettingsService.set_agri_ai_config(test_session, AgriAIConfig(weather_provider='custom', weather_custom=saved, weather_api_key='synthetic-saved-key'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.get('/api/settings')
        assert response.status_code == 200 and response.json()['agri_ai']['has_weather_key']
        assert 'synthetic-saved-key' not in response.text
        await client.post('/api/settings/test-weather-connection', json={'provider': 'custom', 'latitude': 1, 'longitude': 2})
        assert source.calls[-1].headers['Authorization'] == 'Bearer synthetic-saved-key'
        count = len(source.calls)
        changed = {**saved, 'endpoint': 'https://other.example/current'}
        reply = await client.post('/api/settings/test-weather-connection', json={'provider': 'custom', 'latitude': 1, 'longitude': 2, 'weather_custom': changed})
        assert not reply.json()['success'] and len(source.calls) == count
        response = await client.put('/api/settings', json={'agri_ai': {'weather_provider': 'custom', 'weather_custom': changed}})
        assert response.status_code == 200 and not response.json()['agri_ai']['has_weather_key']
        assert (await SettingsService.get_agri_ai_config(test_session)).weather_api_key == ''
        identity.role = 'customer'
        assert (await client.post('/api/settings/test-weather-connection', json={'provider': 'custom'})).status_code == 403


@pytest.mark.asyncio
async def test_probe_requires_explicit_coordinates_and_checks_48h_fields(source):
    success, message = await WeatherService.test_connection(provider='custom', custom_config=config())
    assert not success and not source.calls
    source.body['forecasts'].pop()
    success, message = await WeatherService.test_connection(provider='custom', custom_config=config(), latitude=1, longitude=2)
    assert not success and '不完整' in message


@pytest.mark.asyncio
async def test_open_meteo_protocol_parameters_and_response_units_are_consumed(source):
    from tests.test_agri_optimization_a import provider_data
    source.body = provider_data(days=2)
    result = await CustomWeatherService.fetch(config(format='open_meteo'), '', 1, 2, 2)
    assert result['status'] == 'online'
    assert source.calls[0].url.params['forecast_days'] == '2'
    assert source.calls[0].url.params['wind_speed_unit'] == 'ms'
    assert source.calls[0].url.params['timezone'] == 'Asia/Shanghai'
    assert 'days' not in source.calls[0].url.params


@pytest.mark.asyncio
async def test_probe_coordinate_boolean_rejected_before_transport(test_session, source):
    from app.schemas.settings import TestWeatherRequest
    with pytest.raises(ValidationError): TestWeatherRequest(latitude=True, longitude=2)
    assert not source.calls


@pytest.mark.asyncio
async def test_cache_does_not_outlive_current_observation_or_local_day(source, monkeypatch):
    import time
    source.body['current']['timestamp'] = (datetime.now(AGRI_TIMEZONE) - timedelta(seconds=7199)).isoformat()
    await CustomWeatherService.fetch(config(), '', 1, 2, 2)
    expires, _ = next(iter(CustomWeatherService._cache.values()))
    assert 0 <= expires - time.monotonic() <= 1
    monkeypatch.setattr('app.services.custom_weather_service.agriculture_today', lambda: agriculture_today() + timedelta(days=1))
    await CustomWeatherService.fetch(config(), '', 1, 2, 2)
    assert len(source.calls) == 2


@pytest.mark.asyncio
async def test_custom_field_without_coordinates_is_location_missing_not_unsupported(test_session, source):
    owner = User(username='missing-weather-owner', password_hash='unused', role='customer', is_active=True)
    test_session.add(owner); await test_session.flush()
    farm = Farm(name='no-location', owner_id=owner.id)
    test_session.add(farm); await test_session.flush()
    field = Field(name='no-location-field', farm_id=farm.id)
    test_session.add(field); await test_session.commit()
    await SettingsService.set_agri_ai_config(test_session, AgriAIConfig(weather_provider='custom', weather_custom=config()))
    result = await WeatherService.get_field_weather(test_session, field, 2)
    assert result['status'] == 'location_missing' and result['provider'] == 'custom'
    assert not source.calls


@pytest.mark.asyncio
async def test_manual_refresh_updates_shared_custom_weather_cache(source):
    first = await CustomWeatherService.fetch(config(), '', 1, 2, 2)
    source.body['current']['temperature'] = 23
    refreshed = await CustomWeatherService.fetch(config(), '', 1, 2, 2, force_refresh=True)
    cached = await CustomWeatherService.fetch(config(), '', 1, 2, 2)
    assert first['current']['temperature'] == 20
    assert refreshed['current']['temperature'] == cached['current']['temperature'] == 23
    assert len(source.calls) == 2
