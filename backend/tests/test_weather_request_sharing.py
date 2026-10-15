"""Concurrent weather callers share work without sharing mutable results or cancellation."""
import asyncio
from datetime import timedelta
import pytest
from app.services.weather_service import WeatherService
from app.services import weather_service
from app.services.weather_requests import share_weather_request, _pending


@pytest.mark.asyncio
async def test_open_weather_coalesces_and_caller_cancellation_does_not_cancel_other_waiters(monkeypatch):
    monkeypatch.setattr(weather_service, '_WEATHER_CACHE', {})
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def fetch(latitude, longitude, days):
        calls.append((latitude, longitude, days)); started.set(); await release.wait()
        return WeatherService._fallback_safe_weather(days)
    monkeypatch.setattr(WeatherService, '_fetch_open_meteo', fetch)
    first = asyncio.create_task(WeatherService.get_real_weather(1, 2, 7))
    await started.wait()
    second = asyncio.create_task(WeatherService.get_real_weather(1, 2, 7))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    release.set()
    result = await second
    assert len(calls) == 1 and result['is_fallback']
    result['current']['temperature'] = 99
    cached = await WeatherService.get_real_weather(1, 2, 7)
    assert len(calls) == 1 and cached['current']['temperature'] is None
    assert not cached['spray_safety']['is_suitable_to_spray']


@pytest.mark.asyncio
async def test_weather_cache_keeps_coordinates_days_and_business_date_separate(monkeypatch):
    monkeypatch.setattr(weather_service, '_WEATHER_CACHE', {})
    calls = []
    async def fetch(*args):
        calls.append(args)
        return WeatherService._fallback_safe_weather(args[2])
    monkeypatch.setattr(WeatherService, '_fetch_open_meteo', fetch)
    await WeatherService.get_real_weather(1, 2, 7)
    await WeatherService.get_real_weather(1, 2, 14)
    await WeatherService.get_real_weather(3, 4, 7)
    today = weather_service.agriculture_today()
    monkeypatch.setattr(weather_service, 'agriculture_today', lambda: today + timedelta(days=1))
    await WeatherService.get_real_weather(1, 2, 7)
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_failed_shared_work_is_removed_and_next_request_can_retry():
    calls = []
    async def fetch():
        calls.append(1)
        raise RuntimeError('controlled weather failure')
    for _ in range(2):
        with pytest.raises(RuntimeError): await share_weather_request(('test-failure',), fetch)
    assert len(calls) == 2
    assert not any(key[1] == ('test-failure',) for key in _pending)


@pytest.mark.asyncio
async def test_explicit_weather_refresh_bypasses_and_replaces_cache(monkeypatch):
    monkeypatch.setattr(weather_service, '_WEATHER_CACHE', {})
    calls = []
    async def fetch(latitude, longitude, days):
        calls.append(1)
        result = WeatherService._fallback_safe_weather(days)
        result['summary'] = f'fetch-{len(calls)}'
        return result
    monkeypatch.setattr(WeatherService, '_fetch_open_meteo', fetch)
    assert (await WeatherService.get_real_weather(1, 2, 14))['summary'] == 'fetch-1'
    assert (await WeatherService.get_real_weather(1, 2, 14))['summary'] == 'fetch-1'
    assert (await WeatherService.get_real_weather(1, 2, 14, force_refresh=True))['summary'] == 'fetch-2'
    assert (await WeatherService.get_real_weather(1, 2, 14))['summary'] == 'fetch-2'
    assert len(calls) == 2
