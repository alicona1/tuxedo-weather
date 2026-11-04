"""Server behavior around upstream data: forecast day selection, fallbacks,
stale data, persistence and input validation."""
import datetime
import json
import logging
import re
import xml.etree.ElementTree as ET

import pytest

import app as app_module
from conftest import TZ, ZIP, age_cache, end_backoff, frozen_now, open_meteo_payload


def forecast_names(client):
    root = ET.fromstring(client.post(f'/us/for/{ZIP}.xml').data)
    return [d.findtext('name') for d in root]


def local_today():
    return frozen_now().date()


def weekday(offset):
    return (local_today() + datetime.timedelta(days=offset)).strftime('%A')


# --- Forecast day selection


def test_forecast_is_today_and_next_three_days(client):
    # RefreshScreen() (0x14a248) shows the first <day>'s high/low beside the
    # current conditions and names only the rest, so the first must be today.
    # Starting with tomorrow made the panel's first named day the day after.
    assert forecast_names(client) == [weekday(i) for i in range(0, 4)]


def test_first_day_is_todays_high_and_low(client):
    day = ET.fromstring(client.post(f'/us/for/{ZIP}.xml').data)[0]
    assert (day.findtext('high'), day.findtext('low'), day.findtext('icon')) == ('80', '60', '1')


def test_forecast_starts_today_when_cached_data_starts_yesterday(client):
    # Data cached before midnight, or stale during an outage.
    data = open_meteo_payload(start=local_today() - datetime.timedelta(days=1))
    data.update(city='Chicago, IL', fetched_at='2026-10-21T23:55')
    app_module._cache[ZIP] = {'data': data, 'ts': app_module.time.time()}
    assert forecast_names(client)[0] == weekday(0)


def test_forecast_503_when_cached_data_has_too_few_days(client):
    data = open_meteo_payload(start=local_today() - datetime.timedelta(days=4))
    data.update(city='Chicago, IL', fetched_at='2026-10-18T13:00')
    app_module._cache[ZIP] = {'data': data, 'ts': app_module.time.time()}
    assert client.post(f'/us/for/{ZIP}.xml').status_code == 503


def test_null_forecast_day_is_skipped_and_still_four_days(client, upstream):
    upstream.payload['daily']['temperature_2m_max'][2] = None
    assert forecast_names(client) == [weekday(0), weekday(1), weekday(3), weekday(4)]


def test_local_now_follows_zone_rules_not_cached_offset():
    data = open_meteo_payload()
    data['utc_offset_seconds'] = 0  # stale offset, e.g. cached across a DST change
    assert app_module._local_now(data) == frozen_now().replace(tzinfo=None)


# --- Model and zip lookup fallbacks


def test_uses_nbm_when_available(client, upstream):
    client.post(f'/us/cur/{ZIP}.xml')
    assert upstream.calls == ['zippopotam', 'nbm']


@pytest.mark.parametrize('nbm_mode', ['400', 'nulls', 'daily_nulls', 'down'])
def test_falls_back_to_default_model(client, upstream, nbm_mode):
    # NBM returns HTTP 400 outside the lower 48.
    upstream.nbm = nbm_mode
    r = client.post(f'/us/cur/{ZIP}.xml')
    assert r.status_code == 200
    assert upstream.calls[-2:] == ['nbm', 'default']


@pytest.mark.parametrize('bad', ['nulls', 'daily_nulls'])
def test_null_data_never_replaces_good_cache(client, upstream, bad):
    client.post(f'/us/cur/{ZIP}.xml')
    age_cache(app_module.CACHE_TTL + 1)
    upstream.nbm = upstream.default = bad
    assert client.post(f'/us/cur/{ZIP}.xml').status_code == 200
    assert forecast_names(client) == [weekday(i) for i in range(0, 4)]
    assert json.load(open(app_module.STATE_FILE))['weather'][ZIP]['data']['daily']['temperature_2m_max'][1] is not None


def test_zip_lookup_falls_back_to_open_meteo(client, upstream):
    upstream.zippopotam = 'down'
    root = ET.fromstring(client.post(f'/us/cur/{ZIP}.xml').data)
    assert root.findtext('location') == 'Chicago, IL'
    assert upstream.calls[:2] == ['zippopotam', 'geocode']


def test_geocode_result_must_list_the_zip(client, upstream):
    upstream.zippopotam = 'down'
    assert client.post('/us/cur/12345.xml').status_code == 503


def test_fresh_cache_skips_upstream(client, upstream):
    for endpoint in ('cur', 'for', 'sun'):
        client.post(f'/us/{endpoint}/{ZIP}.xml')
    assert upstream.calls == ['zippopotam', 'nbm']


# --- Stale data and outages


def test_stale_data_served_within_six_hours(client, upstream):
    client.post(f'/us/cur/{ZIP}.xml')
    age_cache(2 * 3600)
    upstream.nbm = upstream.default = 'down'
    assert client.post(f'/us/cur/{ZIP}.xml').status_code == 200


@pytest.mark.parametrize('endpoint', ['cur', 'for'])
def test_cur_and_for_return_503_after_six_hours(client, upstream, endpoint):
    # A non-200 makes the panel keep what it already shows.
    client.post(f'/us/{endpoint}/{ZIP}.xml')
    age_cache(app_module.MAX_STALE + 1)
    upstream.nbm = upstream.default = 'down'
    assert client.post(f'/us/{endpoint}/{ZIP}.xml').status_code == 503


def test_sun_computed_locally_when_data_too_old(client, upstream):
    client.post(f'/us/sun/{ZIP}.xml')
    age_cache(app_module.MAX_STALE + 1)
    upstream.nbm = upstream.default = 'down'
    r = client.post(f'/us/sun/{ZIP}.xml')
    root = ET.fromstring(r.data)
    assert r.status_code == 200
    assert re.fullmatch(r'[5-8]:\d\d AM', root.findtext('sunrise'))
    assert re.fullmatch(r'[4-8]:\d\d PM', root.findtext('sunset'))
    assert root.findtext('date') == '--'


def test_sun_placeholders_when_time_zone_unknown(client, upstream):
    # Known limitation: zippopotam gives coordinates but no time zone, so if
    # Open-Meteo has never answered for this zip, sun times can't be computed.
    upstream.nbm = upstream.default = 'down'
    r = client.post(f'/us/sun/{ZIP}.xml')
    root = ET.fromstring(r.data)
    assert r.status_code == 200
    assert (root.findtext('sunrise'), root.findtext('sunset')) == ('--:--', '--:--')


def test_sun_computed_locally_from_geocode_time_zone(client, upstream):
    # The Open-Meteo geocoding fallback returns a time zone, so the sun
    # endpoint works even if the forecast API has never answered.
    upstream.zippopotam = upstream.nbm = upstream.default = 'down'
    root = ET.fromstring(client.post(f'/us/sun/{ZIP}.xml').data)
    assert (root.findtext('sunrise'), root.findtext('sunset')) == ('7:09 AM', '6:00 PM')


def test_cold_start_without_data_returns_503(client, upstream):
    upstream.nbm = upstream.default = 'down'
    assert client.post(f'/us/cur/{ZIP}.xml').status_code == 503


def test_failed_refresh_backs_off_while_serving_stale(client, upstream):
    # Without a backoff, every request during an outage re-runs the failing
    # upstream calls before falling back, against the panel's 10 s timeout.
    client.post(f'/us/cur/{ZIP}.xml')
    age_cache(app_module.CACHE_TTL + 1)
    upstream.nbm = upstream.default = 'down'
    for endpoint in ('cur', 'for', 'sun', 'cur'):
        assert client.post(f'/us/{endpoint}/{ZIP}.xml').status_code == 200
    assert upstream.calls == ['zippopotam', 'nbm', 'nbm', 'default']

    end_backoff()
    upstream.nbm = 'ok'
    client.post(f'/us/cur/{ZIP}.xml')
    assert upstream.calls[-1] == 'nbm'
    assert app_module._cache[ZIP]['ts'] > app_module.time.time() - 5


def test_failed_cold_fetch_backs_off(client, upstream):
    upstream.nbm = upstream.default = 'down'
    assert client.post(f'/us/cur/{ZIP}.xml').status_code == 503
    calls = len(upstream.calls)
    assert client.post(f'/us/for/{ZIP}.xml').status_code == 503
    assert len(upstream.calls) == calls


# --- Persistence


def test_state_survives_restart(client, upstream):
    client.post(f'/us/cur/{ZIP}.xml')
    state = json.load(open(app_module.STATE_FILE))
    assert state['zips'][ZIP]['timezone'] == TZ
    assert ZIP in state['weather']

    app_module._cache.clear()
    app_module._zip_cache.clear()
    app_module._load_state()
    upstream.zippopotam = upstream.geocode = upstream.nbm = upstream.default = 'down'
    age_cache(app_module.CACHE_TTL + 1)  # force a refresh attempt, which fails
    r = client.post(f'/us/cur/{ZIP}.xml')
    assert r.status_code == 200
    assert ET.fromstring(r.data).findtext('location') == 'Chicago, IL'


def test_caches_are_bounded(client):
    for i in range(app_module.MAX_ZIPS + 5):
        client.post(f'/us/cur/{10000 + i}.xml')
    assert len(app_module._cache) <= app_module.MAX_ZIPS
    assert len(app_module._zip_cache) <= app_module.MAX_ZIPS


# --- Request validation


@pytest.mark.parametrize('zipcode', ['6060', '606010', 'abcde', '60601x'])
@pytest.mark.parametrize('endpoint', ['cur', 'for', 'sun'])
def test_rejects_bad_zip(client, upstream, endpoint, zipcode):
    assert client.post(f'/us/{endpoint}/{zipcode}.xml').status_code == 400
    assert upstream.calls == []


def test_rejects_large_body(client):
    assert client.post(f'/us/cur/{ZIP}.xml', data='x' * 5000).status_code == 413


def test_logged_body_is_escaped(client, caplog):
    with caplog.at_level(logging.INFO):
        client.post(f'/us/cur/{ZIP}.xml', data='line1\nFAKE LOG LINE')
    assert 'line1\\nFAKE LOG LINE' in caplog.text
    assert '\nFAKE LOG LINE' not in caplog.text


# --- Formatting and the local sun calculation


def test_last_update_is_local_fetch_time(client):
    text = ET.fromstring(client.post(f'/us/sun/{ZIP}.xml').data).findtext('date')
    assert text == 'Oct. 22, 2026 1:42 PM'


def test_last_update_may_has_no_period():
    assert app_module._last_update(datetime.datetime(2026, 5, 3, 14, 5)) == 'May 3, 2026 2:05 PM'
    assert app_module._last_update(datetime.datetime(2026, 10, 22, 9, 30)) == 'Oct. 22, 2026 9:30 AM'


@pytest.mark.parametrize('date, sunrise, sunset', [
    ('2026-10-22', '07:11', '17:58'),   # CDT, matches Open-Meteo
    ('2026-11-06', '06:29', '16:38'),   # after DST ends: CST
])
def test_solar_times_chicago(date, sunrise, sunset):
    rise, sset = app_module._solar_times(41.8858, -87.6181, datetime.date.fromisoformat(date), TZ)

    def minutes(t):
        h, m = map(int, t.split(':'))
        return h * 60 + m
    assert abs(rise.hour * 60 + rise.minute - minutes(sunrise)) <= 3
    assert abs(sset.hour * 60 + sset.minute - minutes(sunset)) <= 3


def test_solar_times_polar_night():
    assert app_module._solar_times(71.29, -156.79, datetime.date(2026, 12, 21), 'America/Anchorage') is None
