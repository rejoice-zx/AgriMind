"""Use the actual pinned snapshot; no live provider or production database."""
import pytest
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient, ASGITransport
from app.services import location_service as locations


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError('Known selector regions must resolve without external services')
    monkeypatch.setattr(locations.httpx, 'AsyncClient', no_network)


@pytest.mark.asyncio
@pytest.mark.parametrize('code,province,city,name,lat,lon', [
    ('370783', '山东省', '潍坊市', '寿光市', 36.88, 118.79),
    ('110105', '北京市', '市辖区', '朝阳区', 39.92, 116.44),
    ('220104', '吉林省', '长春市', '朝阳区', 43.84, 125.28),
])
async def test_real_district_resolves_by_code_offline(code, province, city, name, lat, lon):
    response = await locations.search_places(name, code, province, city)
    assert len(response['items']) == 1
    point = response['items'][0]
    assert point['region_code'] == code
    assert point['province'] == province and point['city'] == city
    assert abs(point['latitude'] - lat) < .1 and abs(point['longitude'] - lon) < .1


@pytest.mark.asyncio
async def test_code_and_name_mismatch_is_rejected():
    with pytest.raises(HTTPException) as error:
        await locations.search_places('朝阳区', '110105', '吉林省', '长春市')
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_legacy_municipality_text_returns_canonical_names():
    response = await locations.search_places('朝阳区', province='北京', city='北京')
    assert [point['region_code'] for point in response['items']] == ['110105']
    assert response['items'][0]['city'] == '市辖区'


@pytest.mark.asyncio
async def test_same_name_districts_stay_distinct_without_parent_context():
    response = await locations.search_places('朝阳区')
    assert {point['region_code'] for point in response['items']} == {'110105', '220104'}


@pytest.mark.asyncio
async def test_missing_reference_does_not_invent_or_reuse_another_location(monkeypatch):
    monkeypatch.setattr(locations, 'reference_points', lambda: {})
    response = await locations.search_places('寿光市', '370783', '山东省', '潍坊市')
    assert response['items'] == []


def test_snapshot_has_valid_coordinates_and_matches_selector_codes_and_names():
    expected = {d['code']: d['name'] for p in locations.regions() for c in p['children'] for d in c['children']}
    points = locations.reference_points()
    assert len(points) > 2500
    for code, point in points.items():
        assert point['name'] == expected[code]
        assert 73 < point['longitude'] < 136
        assert 18 < point['latitude'] < 54


@pytest.mark.asyncio
async def test_lookup_then_bind_through_real_api(test_session):
    from app.api.farms import router
    from app.api.dependencies import get_current_user
    from app.database import get_db
    from app.models.user import User
    from app.models.farm import Farm

    owner = User(username='reference-owner', password_hash='synthetic', role='customer', is_active=True)
    test_session.add(owner)
    await test_session.flush()
    farm = Farm(name='Reference test farm', owner_id=owner.id)
    test_session.add(farm)
    await test_session.commit()
    api = FastAPI()
    api.include_router(router)
    async def database():
        yield test_session
    api.dependency_overrides[get_db] = database
    api.dependency_overrides[get_current_user] = lambda: owner
    async with AsyncClient(transport=ASGITransport(app=api), base_url='http://test') as client:
        context = {'name': '寿光市', 'region_code': '370783', 'province': '山东省', 'city': '潍坊市'}
        found = await client.get('/api/locations/search', params=context)
        assert found.status_code == 200
        point = found.json()['items'][0]
        saved = await client.put(f'/api/farms/{farm.id}', json={
            'district': point['name'], 'province': point['province'], 'city': point['city'],
            'region_code': point['region_code'], 'latitude': point['latitude'], 'longitude': point['longitude'],
            'location_source': 'region_reference', 'location_label': point['label'],
        })
        assert saved.status_code == 200, saved.text
        fetched = await client.get(f'/api/farms/{farm.id}')
        assert fetched.json()['latitude'] == point['latitude']
        assert fetched.json()['longitude'] == point['longitude']
        assert fetched.json()['location_source'] == 'region_reference'
