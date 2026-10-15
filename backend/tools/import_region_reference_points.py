"""Build the offline reference-point snapshot from a pinned, MIT-licensed dataset.

Run explicitly when refreshing data. Never used by the application at runtime.
Only code AND name matches with the existing region selector are included.
The source properties.center remains GCJ-02 even in its WGS84 directory, so use
the GCJ-02 directory and explicitly approximate the inverse transformation.
"""
import hashlib
import json
import math
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import urllib.request

REVISION = '3336f3e5fc7a276aa83f4dc0ea502c12c0cd5ac0'
DATA = Path(__file__).resolve().parents[2] / 'backend/app/data'
OUTPUT = DATA / 'china_region_reference_points.json'
LICENSE = DATA / 'china_region_reference_points_LICENSE.txt'


def gcj_offset(lon, lat):
    x, y = lon - 105, lat - 35
    lat_delta = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x))
    lon_delta = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x))
    common = (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    lat_delta += common + (20*math.sin(y*math.pi) + 40*math.sin(y*math.pi/3))*2/3
    lat_delta += (160*math.sin(y*math.pi/12) + 320*math.sin(y*math.pi/30))*2/3
    lon_delta += common + (20*math.sin(x*math.pi) + 40*math.sin(x*math.pi/3))*2/3
    lon_delta += (150*math.sin(x*math.pi/12) + 300*math.sin(x*math.pi/30))*2/3
    rad = math.radians(lat)
    magic = 1 - .00669342162296594323*math.sin(rad)**2
    root = math.sqrt(magic)
    return (lon_delta*180/(6378245/root*math.cos(rad)*math.pi),
            lat_delta*180/((6378245*(1-.00669342162296594323))/(magic*root)*math.pi))


def to_wgs84(lon, lat):
    # Iteratively invert the conventional GCJ offset; approximate reference only.
    result_lon, result_lat = lon, lat
    for _ in range(6):
        dx, dy = gcj_offset(result_lon, result_lat)
        result_lon, result_lat = lon-dx, lat-dy
    return round(result_lon, 6), round(result_lat, 6)


def main():
    if OUTPUT.exists() or LICENSE.exists():
        raise SystemExit('Output already exists; review an update before replacing the snapshot.')
    regions = json.loads((DATA / 'china_regions_2023.json').read_text(encoding='utf-8'))
    names = {d['code']: d['name'] for p in regions for c in p['children'] for d in c['children']}
    points = {}
    def read_url(url):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=25) as response:
            raw = response.read(20_000_001)
        if len(raw) > 20_000_000:
            raise ValueError('Unexpected source file size')
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            return raw.decode('gb18030')

    base = f'https://raw.githubusercontent.com/pamaforce/geojson/{REVISION}/'
    license_text = read_url(base + 'LICENSE')
    tree = json.loads(read_url(f'https://api.github.com/repos/pamaforce/geojson/git/trees/{REVISION}?recursive=1'))
    if tree.get('truncated'):
        raise ValueError('Incomplete repository listing')
    # Province files contain municipality districts; city files contain counties.
    paths = [row['path'] for row in tree['tree'] if row['path'].startswith('geojson_gcj02/')
             and row['path'].endswith('.json') and len(row['path'].split('/')) in (4, 5)]
    def fetch(path):
        return json.loads(read_url(base + path))
    print(f'Fetching {len(paths)} province/city files', flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for index, document in enumerate(pool.map(fetch, paths)):
            for feature in document.get('features', []):
                props = feature.get('properties', {})
                code = str(props.get('adcode', ''))
                if props.get('level') != 'district' or names.get(code) != props.get('name'):
                    continue
                center = props.get('center')
                if not isinstance(center, list) or len(center) != 2:
                    continue
                lon, lat = center
                if not (73 < lon < 136 and 18 < lat < 54):
                    raise ValueError(f'Unexpected coordinate for {code}')
                lon, lat = to_wgs84(lon, lat)
                row = {'name': names[code], 'longitude': lon, 'latitude': lat}
                if code in points and points[code] != row:
                    raise ValueError(f'Conflicting reference points for {code}')
                points[code] = row
            if (index + 1) % 50 == 0:
                print(f'Processed {index + 1}/{len(paths)} files', flush=True)
    if not license_text or len(points) < 2500:
        raise ValueError('Incomplete dataset or missing license')
    content = (json.dumps(dict(sorted(points.items())), ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    with OUTPUT.open('xb') as output:
        output.write(content)
    with LICENSE.open('x', encoding='utf-8') as output:
        output.write(license_text)
    print(json.dumps({'matched': len(points), 'selector_districts': len(names),
                      'sha256': hashlib.sha256(content).hexdigest()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
