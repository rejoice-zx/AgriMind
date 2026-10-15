"""Versioned region selection and bounded, fixed-provider place lookup."""
import asyncio
import json
import re
from functools import lru_cache
from pathlib import Path
import httpx
from fastapi import HTTPException
from app.core.weather_policy import finite_number


@lru_cache(maxsize=1)
def regions():
    return json.loads((Path(__file__).parents[1] / 'data/china_regions_2023.json').read_text(encoding='utf-8'))


def validate_region(code, province, city, district):
    if not code: return
    for p in regions():
        for c in p.get('children', []):
            for d in c.get('children', []):
                if d['code'] == code and (p['name'], c['name'], d['name']) == (province, city, district): return
    raise HTTPException(422, '省市区选择不匹配，请重新选择；未收录地区请使用手动填写')


@lru_cache(maxsize=1)
def reference_points():
    return json.loads((Path(__file__).parents[1] / 'data/china_region_reference_points.json').read_text(encoding='utf-8'))


def normalized_region(name):
    return re.sub(r'(壮族自治区|回族自治区|维吾尔自治区|自治区|特别行政区|省|市|县|区)$', '', (name or '').strip())


async def search_places(name, region_code=None, province=None, city=None):
    """Resolve selector regions locally, with country-filtered legacy name lookup."""
    if region_code:
        validate_region(region_code, province, city, name)
    items = []
    known_region = False
    for p in regions():
        if province and normalized_region(province) != normalized_region(p['name']):
            continue
        for c in p.get('children', []):
            # The selector represents municipalities with a virtual 市辖区 node.
            city_names = [c['name']]
            if p['code'] in ('11', '12', '31', '50') and c['name'] in ('市辖区', '县'):
                city_names.append(p['name'])
            if city and normalized_region(city) not in [normalized_region(n) for n in city_names]:
                continue
            for d in c.get('children', []):
                if region_code and d['code'] != region_code:
                    continue
                # Do not conflate 朝阳区 with 朝阳县. Also accept legacy suffix-free text.
                if name.strip() not in (d['name'], normalized_region(d['name'])):
                    continue
                known_region = True
                point = reference_points().get(d['code'])
                if not point or point['name'] != d['name']:
                    continue
                items.append({**point, 'region_code': d['code'], 'province': p['name'], 'city': c['name'],
                              'label': ' / '.join((p['name'], c['name'], d['name']))})
    if known_region:
        return {'items': items, 'source': '本地区划参考点，近似WGS84（非农场精确地址）'}

    try:
        async with asyncio.timeout(8):
            async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
                async with client.stream('GET', 'https://geocoding-api.open-meteo.com/v1/search',
                    params={'name': name, 'count': 10, 'language': 'zh', 'countryCode': 'CN'},
                    headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'}) as response:
                    if response.status_code != 200: raise ValueError('provider unavailable')
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        body.extend(chunk)
                        if len(body) > 131072: raise ValueError('response too large')
                    data = json.loads(body)
        items = []
        for row in data.get('results', [])[:10]:
            lat, lon = finite_number(row.get('latitude'), -90, 90), finite_number(row.get('longitude'), -180, 180)
            if row.get('country_code') != 'CN' or lat is None or lon is None: continue
            if province and normalized_region(row.get('admin1')) != normalized_region(province): continue
            if city and normalized_region(row.get('admin2')) != normalized_region(city): continue
            label = ' / '.join(str(row.get(k) or '')[:80] for k in ('admin1', 'admin2', 'name'))
            items.append({'latitude': lat, 'longitude': lon, 'name': str(row.get('name') or '')[:80],
                'province': str(row.get('admin1') or '')[:80], 'city': str(row.get('admin2') or '')[:80], 'label': label})
        return {'items': items, 'source': 'Open-Meteo / GeoNames，WGS84区域参考点（非农场精确地址）'}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, TimeoutError):
        raise HTTPException(503, '地点查询暂不可用，请填写实际经纬度；未自动绑定演示位置') from None
