"""Current conditions from the nearest NWS station, with the model as fallback."""
import json
import xml.etree.ElementTree as ET

import app as app_module
import pytest

from conftest import STATION, ZIP, age_cache, end_nws_backoff


def current(client):
    root = ET.fromstring(client.post(f'/us/cur/{ZIP}.xml').data)
    return {c.tag: c.text for c in root}


def test_current_conditions_come_from_station(client):
    # 14 C, 87.7%, 9 km/h from 200 degrees; the model says 75 F, 68%, 5 mph NE.
    cur = current(client)
    assert (cur['temp'], cur['rhumid'], cur['windspeed'], cur['wind_dir']) == ('57', '88', '6', 'S')


def test_icon_still_comes_from_model(client):
    assert current(client)['icon'] == '1'


def test_model_used_when_nws_down(client, upstream):
    upstream.nws = 'down'
    r = client.post(f'/us/cur/{ZIP}.xml')
    assert r.status_code == 200
    root = ET.fromstring(r.data)
    assert (root.findtext('temp'), root.findtext('rhumid')) == ('75', '68')
    # An NWS failure is not an upstream failure: no backoff, data is fresh.
    assert app_module._retry_after == {}


def test_stale_observation_ignored(client, upstream):
    upstream.nws = 'stale'
    assert current(client)['temp'] == '75'


@pytest.mark.parametrize('mode', ['nulls', 'null_object', 'missing'])
def test_skips_bad_report_and_uses_older_one(client, upstream, mode):
    upstream.nws = mode
    assert current(client)['temp'] == '59'  # the older report: 15 C


def test_no_stations_falls_back_to_model(client, upstream):
    upstream.nws = 'no_stations'
    assert current(client)['temp'] == '75'
    assert 'station' not in app_module._zip_cache[ZIP]


def test_calm_wind_keeps_model_direction(client, upstream):
    upstream.nws = 'calm'
    cur = current(client)
    assert (cur['windspeed'], cur['wind_dir']) == ('0', 'NE')


def test_station_looked_up_once_and_saved(client, upstream):
    client.post(f'/us/cur/{ZIP}.xml')
    assert upstream.nws_calls == [
        '/points/41.8858,-87.6181',  # NWS redirects more than 4 decimals
        '/gridpoints/LOT/76,73/stations',
        f'/stations/{STATION}/observations',
    ]
    state = json.load(open(app_module.STATE_FILE))
    assert state['zips'][ZIP]['station'] == STATION
    assert state['weather'][ZIP]['data']['observation']['station'] == STATION

    age_cache(app_module.CACHE_TTL + 1)
    upstream.nws_calls.clear()
    client.post(f'/us/cur/{ZIP}.xml')
    assert upstream.nws_calls == [f'/stations/{STATION}/observations']


def test_nws_failure_backs_off(client, upstream):
    # Every refresh during an NWS outage would otherwise wait out its timeout.
    upstream.nws = 'down'
    client.post(f'/us/cur/{ZIP}.xml')
    assert upstream.nws_calls
    upstream.nws_calls.clear()
    age_cache(app_module.CACHE_TTL + 1)
    assert current(client)['temp'] == '75'
    assert upstream.nws_calls == []


def test_failed_station_lookup_retried_after_backoff(client, upstream):
    upstream.nws = 'down'
    client.post(f'/us/cur/{ZIP}.xml')
    assert 'station' not in app_module._zip_cache[ZIP]

    upstream.nws = 'ok'
    age_cache(app_module.CACHE_TTL + 1)
    end_nws_backoff()
    assert current(client)['temp'] == '57'
    assert app_module._nws_retry_after == {}


def test_health_shows_station(client):
    client.post(f'/us/cur/{ZIP}.xml')
    assert client.get('/health').get_json()['stations'] == {ZIP: STATION}
