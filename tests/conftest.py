import atexit
import copy
import datetime
import os
import shutil
import tempfile
from zoneinfo import ZoneInfo

import pytest
import requests

# app.py reads CACHE_DIR and loads saved state at import time. Always point it
# at a throwaway directory, even if CACHE_DIR is set in the environment.
_TEST_CACHE_DIR = tempfile.mkdtemp(prefix='tuxedo-test-')
atexit.register(shutil.rmtree, _TEST_CACHE_DIR, ignore_errors=True)
os.environ['CACHE_DIR'] = _TEST_CACHE_DIR

import app as app_module  # noqa: E402

TZ = 'America/Chicago'
ZIP = '60601'
# Every test runs at this instant (1:42 PM CDT, Thursday Oct. 22, 2026), so
# results never depend on the real clock, e.g. a run crossing midnight.
FROZEN_NOW = datetime.datetime(2026, 10, 22, 18, 42, tzinfo=datetime.timezone.utc)


def frozen_now(tz=TZ):
    return FROZEN_NOW.astimezone(ZoneInfo(tz))


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error', response=self)


def open_meteo_payload(start=None, days=7, tz=TZ):
    """An Open-Meteo forecast response whose daily list starts on `start`
    (default: today at the location)."""
    now = frozen_now(tz)
    start = start or now.date()
    dates = [start + datetime.timedelta(days=i) for i in range(days)]
    codes = [0, 1, 2, 3, 61, 95, 71]
    return {
        'timezone': tz,
        'utc_offset_seconds': int(now.utcoffset().total_seconds()),
        'current': {
            'temperature_2m': 75.4,
            'relative_humidity_2m': 68,
            'wind_speed_10m': 5.3,
            'wind_direction_10m': 34,
            'weather_code': 0,
        },
        'daily': {
            'time': [d.isoformat() for d in dates],
            'temperature_2m_max': [80.4 + i for i in range(days)],
            'temperature_2m_min': [60.4 + i for i in range(days)],
            'weather_code': [codes[i % len(codes)] for i in range(days)],
            'sunrise': [f'{d.isoformat()}T07:27' for d in dates],
            'sunset': [f'{d.isoformat()}T18:43' for d in dates],
        },
    }


STATION = 'KMDW'


def nws_observations(mode='ok'):
    """NWS /stations/KMDW/observations response, newest first. Default
    reading: 14 C (57 F), 87.7% RH, wind 9 km/h (6 mph) from 200 degrees."""
    def obs(minutes_ago, temp_c=14.0, speed=9.0, direction=200):
        taken = FROZEN_NOW - datetime.timedelta(minutes=minutes_ago)
        return {'properties': {
            'timestamp': taken.isoformat(),
            'temperature': {'unitCode': 'wmoUnit:degC', 'value': temp_c},
            'relativeHumidity': {'unitCode': 'wmoUnit:percent', 'value': 87.7},
            'windSpeed': {'unitCode': 'wmoUnit:km_h-1', 'value': speed},
            'windDirection': {'unitCode': 'wmoUnit:degree_(angle)', 'value': direction},
        }}
    if mode == 'stale':
        features = [obs(120), obs(180)]
    elif mode == 'nulls':
        features = [obs(5, temp_c=None), obs(10, temp_c=15.0)]
    elif mode == 'null_object':
        features = [obs(5), obs(10, temp_c=15.0)]
        features[0]['properties']['temperature'] = None
    elif mode == 'missing':
        features = [obs(5), obs(10, temp_c=15.0)]
        del features[0]['properties']['temperature']
        del features[0]['properties']['timestamp']
    elif mode == 'calm':
        features = [obs(5, speed=0.0, direction=0)]
    else:
        features = [obs(5), obs(10)]
    return {'features': features}


class Upstream:
    """Stands in for zippopotam.us and Open-Meteo. Each service is 'ok',
    'down' (connection error) or '400'. Open-Meteo models can also be 'nulls'
    (null in current conditions) or 'daily_nulls' (null highs/lows). NWS can
    also be 'stale', 'nulls' (newest report's temperature value is null),
    'null_object' (newest temperature is null), 'missing' (newest report has
    no temperature or timestamp), 'calm' or 'no_stations'.
    NWS calls are recorded separately in nws_calls."""

    def __init__(self):
        self.zippopotam = 'ok'
        self.geocode = 'ok'
        self.nbm = 'ok'
        self.default = 'ok'
        self.nws = 'ok'
        self.place_name = 'Chicago'
        self.calls = []
        self.nws_calls = []
        self.payload = open_meteo_payload()

    def get(self, url, params=None, timeout=None, headers=None):
        if url.startswith(app_module.NWS_API):
            return self._nws(url, timeout, headers)
        assert timeout == app_module.HTTP_TIMEOUT, 'every upstream call needs the short timeout'
        if url.startswith('https://api.zippopotam.us/'):
            return self._respond('zippopotam', {'places': [{
                'place name': self.place_name, 'state abbreviation': 'IL',
                'latitude': '41.8858', 'longitude': '-87.6181',
            }]})
        if url == app_module.GEOCODE_URL:
            return self._respond('geocode', {'results': [{
                'name': self.place_name, 'admin1': 'Illinois',
                'postcodes': ['60601', '60602'],
                'latitude': 41.85003, 'longitude': -87.65005,
                'timezone': TZ,
            }]})
        if url == app_module.OPEN_METEO_URL:
            name = 'nbm' if params.get('models') == app_module.OPEN_METEO_MODEL else 'default'
            payload = copy.deepcopy(self.payload)
            if getattr(self, name) == 'nulls':
                payload['current']['temperature_2m'] = None
            if getattr(self, name) == 'daily_nulls':
                for field in ('temperature_2m_max', 'temperature_2m_min'):
                    payload['daily'][field][1:] = [None] * (len(payload['daily'][field]) - 1)
            return self._respond(name, payload)
        raise AssertionError(f'unexpected upstream URL {url}')

    def _nws(self, url, timeout, headers):
        assert timeout == app_module.NWS_TIMEOUT
        assert 'tuxedo-weather' in headers['User-Agent'], 'NWS requires a User-Agent'
        path = url[len(app_module.NWS_API):]
        self.nws_calls.append(path.split('?')[0])
        if self.nws == 'down':
            raise requests.ConnectionError('NWS unreachable')
        if path.startswith('/points/'):
            return FakeResponse({'properties': {
                'observationStations': f'{app_module.NWS_API}/gridpoints/LOT/76,73/stations'}})
        if path.endswith('/stations'):
            if self.nws == 'no_stations':
                return FakeResponse({'features': []})
            return FakeResponse({'features': [
                {'properties': {'stationIdentifier': STATION}},
                {'properties': {'stationIdentifier': 'KORD'}},
            ]})
        if path.startswith(f'/stations/{STATION}/observations'):
            return FakeResponse(nws_observations(self.nws))
        raise AssertionError(f'unexpected NWS URL {url}')

    def _respond(self, name, payload):
        self.calls.append(name)
        mode = getattr(self, name)
        if mode in ('nulls', 'daily_nulls'):
            mode = 'ok'
        if mode == 'down':
            raise requests.ConnectionError(f'{name} unreachable')
        if mode == '400':
            return FakeResponse({'error': True, 'reason': 'No data is available for this location'}, 400)
        return FakeResponse(payload)


def _clear_caches():
    app_module._cache.clear()
    app_module._zip_cache.clear()
    app_module._retry_after.clear()
    app_module._nws_retry_after.clear()


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    _clear_caches()
    monkeypatch.setattr(app_module, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(app_module, '_now', frozen_now)
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def upstream(monkeypatch):
    # Autouse so no test can reach the real network.
    fake = Upstream()
    monkeypatch.setattr(app_module.requests, 'get', fake.get)
    return fake


@pytest.fixture
def client():
    return app_module.app.test_client()


def age_cache(seconds, zipcode=ZIP):
    app_module._cache[zipcode]['ts'] -= seconds


def end_backoff(zipcode=ZIP):
    app_module._retry_after.pop(zipcode, None)


def end_nws_backoff(zipcode=ZIP):
    app_module._nws_retry_after.pop(zipcode, None)
