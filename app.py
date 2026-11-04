from flask import Flask, Response, jsonify, request
import requests
from werkzeug.serving import WSGIRequestHandler
import xml.etree.ElementTree as ET
import datetime
import json
import logging
import math
import os
import re
import signal
import threading
import time
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

app = Flask(__name__)
# The panel POSTs with an empty body; nothing legitimate is larger than this.
app.config['MAX_CONTENT_LENGTH'] = 4096

# NOAA National Blend of Models via Open-Meteo. Free, no API key. Tracked the
# iPhone (Apple Weather) daily highs/lows within 1°F in a 5-day comparison.
# NBM only covers the lower 48; anywhere else Open-Meteo rejects the request,
# so we fall back to Open-Meteo's default model.
OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'
OPEN_METEO_MODEL = 'ncep_nbm_conus'
ZIP_LOOKUP_URL = 'https://api.zippopotam.us/us/{zipcode}'
GEOCODE_URL = 'https://geocoding-api.open-meteo.com/v1/search'

# Current conditions come from the nearest National Weather Service station
# (e.g. KMDW, Chicago Midway, for 60601): measured values instead of
# NBM's model estimate, which ran about 3°F warm on a calm morning. The model
# fills in any field the station can't provide, and the icon always comes
# from the model. NWS requires a User-Agent identifying the client, and asks
# for contact details so it can reach whoever runs it. That's the operator of
# each copy of this server, so it comes from NWS_CONTACT (an email or URL).
NWS_API = 'https://api.weather.gov'


def _nws_user_agent(contact: str) -> str:
    contact = ' '.join(contact.split())  # no CR/LF: it goes in a header
    return f'tuxedo-weather ({contact})' if contact else 'tuxedo-weather (self-hosted; NWS_CONTACT not set)'


NWS_HEADERS = {
    'User-Agent': _nws_user_agent(os.environ.get('NWS_CONTACT', '')),
    'Accept': 'application/geo+json',
}
MAX_OBS_AGE = 90 * 60    # ignore observations older than this
# After an NWS failure, skip it for this long. NWS failures don't trigger the
# main backoff (the model still answers), so without this every refresh
# during an NWS outage would pay the NWS timeout again.
NWS_RETRY_BACKOFF = 10 * 60

# The panel's get_data() (0x485bc) sets a 10 s socket timeout and retries
# recv() once, so every upstream call has to finish well inside that.
HTTP_TIMEOUT = (2, 3)  # connect, read
# Shorter for NWS: it's optional, and it adds to the Open-Meteo call.
NWS_TIMEOUT = (2, 2)

# The panel's DemoWeatherData() shows "Melville, NY", so <location> is "City, ST".
US_STATES = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR',
    'California': 'CA', 'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE',
    'District of Columbia': 'DC', 'Florida': 'FL', 'Georgia': 'GA', 'Hawaii': 'HI',
    'Idaho': 'ID', 'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA', 'Kansas': 'KS',
    'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
    'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS',
    'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV',
    'New Hampshire': 'NH', 'New Jersey': 'NJ', 'New Mexico': 'NM', 'New York': 'NY',
    'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK',
    'Oregon': 'OR', 'Pennsylvania': 'PA', 'Puerto Rico': 'PR', 'Rhode Island': 'RI',
    'South Carolina': 'SC', 'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX',
    'Utah': 'UT', 'Vermont': 'VT', 'Virginia': 'VA', 'Washington': 'WA',
    'West Virginia': 'WV', 'Wisconsin': 'WI', 'Wyoming': 'WY',
}

ZIP_RE = re.compile(r'\d{5}')
CURRENT_FIELDS = ('temperature_2m', 'relative_humidity_2m', 'wind_speed_10m',
                  'wind_direction_10m', 'weather_code')

CACHE_TTL = 600          # refresh after 10 minutes
MAX_STALE = 6 * 3600     # serve old data for up to 6 hours if upstream is down
RETRY_BACKOFF = 60       # after a failed refresh, don't retry upstream for this long
# <day> elements: today, then three named days. RefreshScreen() shows the
# first <day>'s high/low beside the current conditions (no name), and the rest
# as named forecast days, so the first one must be today.
FORECAST_DAYS = 4
MAX_ZIPS = 32            # the panel only ever asks for one zip
STATE_FILE = os.path.join(os.environ.get('CACHE_DIR', '/data'), 'state.json')

_cache: dict = {}        # zip -> {'data': open-meteo response, 'ts': epoch}
_zip_cache: dict = {}    # zip -> {'city', 'lat', 'lon', 'timezone'}
_retry_after: dict = {}  # zip -> epoch before which upstream isn't retried
_nws_retry_after: dict = {}  # zip -> epoch before which NWS isn't retried
# One lock for both caches. Fetches are serialized, which also coalesces the
# panel's back-to-back requests into a single upstream call.
_lock = threading.Lock()


def _load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        _zip_cache.update(state.get('zips', {}))
        _cache.update(state.get('weather', {}))
        log.info('Loaded saved state for zips %s', list(_zip_cache))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception('Could not load %s, starting empty', STATE_FILE)


def _save_state():
    # Persisted so a container restart during an internet outage still has
    # coordinates and recent weather. Caller holds _lock.
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'zips': _zip_cache, 'weather': _cache}, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        log.exception('Could not save %s', STATE_FILE)


def _remember(cache: dict, key: str, value):
    cache.pop(key, None)
    while len(cache) >= MAX_ZIPS:
        cache.pop(next(iter(cache)))
    cache[key] = value


def _cached(zipcode: str, fetch_fn):
    with _lock:
        entry = _cache.get(zipcode)
        age = time.time() - entry['ts'] if entry else None
        if entry and age < CACHE_TTL:
            return entry['data']
        usable = entry is not None and age < MAX_STALE
        # A failed refresh would otherwise be retried on every request, and
        # each retry can take several seconds against the panel's 10 s timeout.
        if time.time() < _retry_after.get(zipcode, 0):
            if usable:
                return entry['data']
            raise LookupError(f'upstream for {zipcode} failed recently, backing off')
        try:
            data = fetch_fn()
        except Exception:
            _remember(_retry_after, zipcode, time.time() + RETRY_BACKOFF)
            if usable:
                log.exception('Fetch failed for %s, serving stale data from %ds ago',
                              zipcode, age)
                return entry['data']
            raise
        _retry_after.pop(zipcode, None)
        _remember(_cache, zipcode, {'data': data, 'ts': time.time()})
        _save_state()
        return data


def _wind_cardinal(deg: float) -> str:
    dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
    return dirs[round(deg / 45) % 8]


def _panel_icon(wmo_code: int) -> str:
    # The panel's extractIcon() (0x57290) indexes a table at 0xd089a8 with the
    # <icon> value, using AccuWeather icon codes 1-44 (out of range -> cloudy).
    # Image each code resolves to, from that table:
    #   1 sunny, 2-3 mostly_sunny, 6 mostly_cloudy, 7 cloudy, 11 fog,
    #   12 storm (showers), 15 thunderstorm, 18 rain, 19 chance_of_snow,
    #   22 snow, 24 icy, 25 sleet, 32 windy, 37 haze. 9, 10, 27, 28 are NULL.
    # Input is a WMO weather code as returned by Open-Meteo.
    if wmo_code == 0:
        return '1'   # clear -> sunny
    if wmo_code == 1:
        return '2'   # mainly clear -> mostly sunny
    if wmo_code == 2:
        return '3'   # partly cloudy -> mostly sunny
    if wmo_code == 3:
        return '7'   # overcast -> cloudy
    if wmo_code in (45, 48):
        return '11'  # fog
    if wmo_code in (51, 53, 55, 61, 63, 65):
        return '18'  # drizzle / rain -> rain
    if wmo_code in (56, 57, 66, 67):
        return '24'  # freezing drizzle / rain -> icy
    if wmo_code in (80, 81, 82):
        return '12'  # rain showers -> storm (Honeywell's showers image)
    if wmo_code in (71, 77, 85):
        return '19'  # light snow / grains / snow showers -> chance of snow
    if wmo_code in (73, 75, 86):
        return '22'  # snow
    if wmo_code in (95, 96, 99):
        return '15'  # thunderstorm
    return '7'


def _clock(t: datetime.datetime) -> str:
    # The panel displays sunrise/sunset text as-is, but parse_sunncs_data
    # (0x64c1c) also does hour = strtol(s), minute = strtol(s + 2), and
    # fetch_today_sun_time() (0x40a50) adds 12 to the sunset hour. So it
    # expects 12-hour time with no leading zero ("6:43 PM"). A two-digit hour
    # (10-12) makes the panel read the minute as 0. That's a firmware limit:
    # it only bites where summer sunset is after 10 PM, e.g. the far western
    # edges of northern time zones.
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _last_update(t: datetime.datetime) -> str:
    # Shown verbatim after "Last Update: "; nothing in the firmware parses it.
    month = 'May' if t.month == 5 else t.strftime('%b') + '.'
    return f'{month} {t.day}, {t.year} {_clock(t)}'


def _now(tz: str) -> datetime.datetime:
    # The one place the server reads the wall clock for dates; tests freeze it.
    return datetime.datetime.now(ZoneInfo(tz))


def _local_now(data: dict) -> datetime.datetime:
    # Naive local time at the weather location, computed at request time so
    # cached data never decides what "today" is. Uses the zone's rules rather
    # than the response's utc_offset_seconds, which goes stale across DST changes.
    return _now(data['timezone']).replace(tzinfo=None)


def _forecast_days(data: dict) -> list:
    # (date, high, low, code) for complete days from today on at the location.
    daily = data['daily']
    today = _local_now(data).date().isoformat()
    return [d for d in zip(daily['time'], daily['temperature_2m_max'],
                           daily['temperature_2m_min'], daily['weather_code'])
            if d[0] >= today and d[1] is not None and d[2] is not None]


def _solar_times(lat: float, lon: float, day: datetime.date, tz: str):
    # NOAA's sunrise equation, accurate to about a minute. Used when
    # Open-Meteo is unreachable, so the sun endpoint (fetched at panel
    # startup and once a day) still gets real times. Returns None during polar day/night.
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    phi = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl))
              - math.tan(phi) * math.tan(decl))
    if not -1 <= cos_ha <= 1:
        return None
    ha = math.degrees(math.acos(cos_ha))
    midnight = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)
    zone = ZoneInfo(tz)

    def at(minutes):
        return (midnight + datetime.timedelta(minutes=minutes)).astimezone(zone)

    return at(720 - 4 * (lon + ha) - eqtime), at(720 - 4 * (lon - ha) - eqtime)


def _city_state(city: str, state: str) -> str:
    return f'{city}, {state}' if state else city


def _lookup_zip_zippopotam(zipcode: str) -> dict:
    r = requests.get(ZIP_LOOKUP_URL.format(zipcode=zipcode), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    place = r.json()['places'][0]
    return {
        'city': _city_state(place['place name'], place.get('state abbreviation', '')),
        'lat': float(place['latitude']),
        'lon': float(place['longitude']),
    }


def _lookup_zip_open_meteo(zipcode: str) -> dict:
    r = requests.get(GEOCODE_URL, params={
        'name': zipcode, 'countryCode': 'US', 'count': 10,
    }, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    # Name search can match places that merely contain the digits; only
    # accept a result that actually lists this zip code.
    for place in r.json().get('results', []):
        if zipcode in place.get('postcodes', []):
            return {
                'city': _city_state(place['name'], US_STATES.get(place.get('admin1', ''), '')),
                'lat': place['latitude'],
                'lon': place['longitude'],
                # zippopotam has no time zone; this lets the sun endpoint
                # compute times locally even before a forecast fetch succeeds.
                'timezone': place.get('timezone'),
            }
    raise LookupError(f'Open-Meteo geocoding has no match for zip {zipcode}')


def _lookup_zip(zipcode: str) -> dict:
    # Caller holds _lock.
    if zipcode not in _zip_cache:
        try:
            loc = _lookup_zip_zippopotam(zipcode)
        except Exception as e:
            log.warning('zippopotam lookup failed for %s (%s), trying Open-Meteo', zipcode, e)
            loc = _lookup_zip_open_meteo(zipcode)
        _remember(_zip_cache, zipcode, loc)
        _save_state()
    return _zip_cache[zipcode]


def _get_open_meteo(loc: dict, model: str = None) -> dict:
    params = {
        'latitude': loc['lat'],
        'longitude': loc['lon'],
        'current': ','.join(CURRENT_FIELDS),
        'daily': 'temperature_2m_max,temperature_2m_min,weather_code,sunrise,sunset',
        'temperature_unit': 'fahrenheit',
        'wind_speed_unit': 'mph',
        'timezone': 'auto',
        'forecast_days': 7,
    }
    if model:
        params['models'] = model
    r = requests.get(OPEN_METEO_URL, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    # Check before the response can reach the cache: a null here would
    # otherwise replace good data and break every request until the next refresh.
    name = model or 'default'
    missing = [f for f in CURRENT_FIELDS if data.get('current', {}).get(f) is None]
    if missing:
        raise ValueError(f'Open-Meteo {name} response missing current {missing}')
    try:
        days = _forecast_days(data)
        daily = data['daily']
        today = daily['time'].index(_local_now(data).date().isoformat())
        sun_ok = daily['sunrise'][today] and daily['sunset'][today]
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f'Open-Meteo {name} response has unusable daily data: {e}')
    if len(days) < FORECAST_DAYS or not sun_ok:
        raise ValueError(f'Open-Meteo {name} response has {len(days)} complete '
                         f'forecast days, sun times {"ok" if sun_ok else "missing"}')
    return data


def _nws_get(url: str) -> dict:
    if url.startswith('/'):
        url = NWS_API + url
    r = requests.get(url, headers=NWS_HEADERS, timeout=NWS_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _nws_station(loc: dict) -> str:
    # Nearest observation station, looked up once per zip and saved.
    # Caller holds _lock.
    if not loc.get('station'):
        # NWS redirects requests with more than 4 decimal places.
        points = _nws_get(f"/points/{loc['lat']:.4f},{loc['lon']:.4f}")
        stations = _nws_get(points['properties']['observationStations']).get('features') or []
        if not stations:
            raise LookupError(f"NWS lists no observation stations near {loc['lat']},{loc['lon']}")
        loc['station'] = stations[0]['properties']['stationIdentifier']
        _save_state()
    return loc['station']


def _nws_observation(station: str):
    # Newest observation with a temperature, if it's recent enough. Stations
    # sometimes publish reports with null fields, so look back a few.
    now = _now('UTC')
    for feature in _nws_get(f'/stations/{station}/observations?limit=6').get('features') or []:
        obs = feature.get('properties') or {}
        # A bad report (null or missing temperature, no timestamp) shouldn't
        # stop the search; an older report may be fine.
        if (obs.get('temperature') or {}).get('value') is None or not obs.get('timestamp'):
            continue
        taken = datetime.datetime.fromisoformat(obs['timestamp'])
        if (now - taken).total_seconds() > MAX_OBS_AGE:
            return None
        return obs
    return None


def _value(obs: dict, field: str, conversions: dict):
    measure = obs.get(field) or {}
    value, unit = measure.get('value'), measure.get('unitCode', '')
    if value is None:
        return None
    convert = conversions.get(unit.split(':')[-1])
    return convert(value) if convert else None


def _apply_observation(data: dict, obs: dict, station: str):
    cur = data['current']
    temp = _value(obs, 'temperature', {'degC': lambda c: c * 9 / 5 + 32, 'degF': lambda f: f})
    rh = _value(obs, 'relativeHumidity', {'percent': lambda p: p})
    speed = _value(obs, 'windSpeed', {'km_h-1': lambda k: k / 1.609344,
                                      'm_s-1': lambda m: m * 2.236936})
    direction = _value(obs, 'windDirection', {'degree_(angle)': lambda d: d})
    if temp is not None:
        cur['temperature_2m'] = temp
    if rh is not None:
        cur['relative_humidity_2m'] = rh
    if speed is not None:
        cur['wind_speed_10m'] = speed
        # Calm is reported as direction 0 (north); keep the model's direction.
        if speed > 0 and direction is not None:
            cur['wind_direction_10m'] = direction
    data['observation'] = {'station': station, 'time': obs['timestamp']}


def _fetch_weather(zipcode: str) -> dict:
    loc = _lookup_zip(zipcode)
    try:
        data = _get_open_meteo(loc, OPEN_METEO_MODEL)
    except Exception as e:
        log.warning('%s failed for %s (%s), using default model', OPEN_METEO_MODEL, zipcode, e)
        data = _get_open_meteo(loc)
    if time.time() < _nws_retry_after.get(zipcode, 0):
        log.info('NWS failed recently, using model for %s', zipcode)
    else:
        try:
            station = _nws_station(loc)
            obs = _nws_observation(station)
            _nws_retry_after.pop(zipcode, None)
            if obs:
                _apply_observation(data, obs, station)
                log.info('Current conditions for %s from %s at %s', zipcode, station, obs['timestamp'])
            else:
                log.warning('No recent observation from %s, using model for %s', station, zipcode)
        except Exception as e:
            _remember(_nws_retry_after, zipcode, time.time() + NWS_RETRY_BACKOFF)
            log.warning('NWS observation failed for %s (%s), using model', zipcode, e)
    data['city'] = loc['city']
    data['fetched_at'] = _local_now(data).isoformat(timespec='minutes')
    if loc.get('timezone') != data.get('timezone'):
        loc['timezone'] = data.get('timezone')
        _save_state()
    return data


def _xml_response(root: ET.Element) -> Response:
    xml_bytes = ET.tostring(root, encoding='unicode').encode('utf-8')
    log.info('Response: %s', xml_bytes.decode())
    resp = Response(xml_bytes, mimetype='text/xml')
    resp.headers['Content-Length'] = str(len(xml_bytes))
    return resp


def _unavailable() -> Response:
    # A non-200 makes the panel skip parsing and keep what it already shows,
    # instead of overwriting it with placeholders.
    return Response('weather unavailable', status=503, mimetype='text/plain')


def _log_request():
    body = request.get_data(as_text=True)[:256]
    log.info(
        '%s %s from %s | headers: %s | body: %s',
        request.method,
        request.path,
        request.remote_addr,
        dict(request.headers),
        body.encode('unicode_escape').decode() if body else '<empty>',
    )


def _bad_zip(zipcode: str):
    if not ZIP_RE.fullmatch(zipcode):
        return Response('zip code must be 5 digits', status=400, mimetype='text/plain')
    return None


@app.route('/health')
def health():
    with _lock:
        zips = {z: loc.get('station') for z, loc in _zip_cache.items()}
    return jsonify({'status': 'ok', 'model': OPEN_METEO_MODEL, 'stations': zips})


@app.route('/us/cur/<zipcode>.xml', methods=['POST', 'GET'])
def get_current_weather(zipcode):
    _log_request()
    if (bad := _bad_zip(zipcode)):
        return bad
    try:
        data = _cached(zipcode, lambda: _fetch_weather(zipcode))
    except Exception as e:
        log.error('Current weather unavailable for %s: %s', zipcode, e)
        return _unavailable()
    cur = data['current']
    root = ET.Element('current')
    ET.SubElement(root, 'location').text = data['city']
    ET.SubElement(root, 'temp').text = str(round(cur['temperature_2m']))
    ET.SubElement(root, 'rhumid').text = str(round(cur['relative_humidity_2m']))
    ET.SubElement(root, 'wind_dir').text = _wind_cardinal(cur['wind_direction_10m'])
    ET.SubElement(root, 'windspeed').text = str(round(cur['wind_speed_10m']))
    ET.SubElement(root, 'icon').text = _panel_icon(cur['weather_code'])
    return _xml_response(root)


@app.route('/us/for/<zipcode>.xml', methods=['POST', 'GET'])
def get_forecast_weather(zipcode):
    _log_request()
    if (bad := _bad_zip(zipcode)):
        return bad
    try:
        data = _cached(zipcode, lambda: _fetch_weather(zipcode))
    except Exception as e:
        log.error('Forecast unavailable for %s: %s', zipcode, e)
        return _unavailable()
    days = _forecast_days(data)[:FORECAST_DAYS]
    if len(days) < FORECAST_DAYS:
        # Only possible with stale data; keep what the panel already shows.
        log.error('Forecast for %s has only %d usable days', zipcode, len(days))
        return _unavailable()
    root = ET.Element('forecast')
    for date_str, high, low, code in days:
        day_elem = ET.SubElement(root, 'day')
        ET.SubElement(day_elem, 'name').text = datetime.date.fromisoformat(date_str).strftime('%A')
        ET.SubElement(day_elem, 'high').text = str(round(high))
        ET.SubElement(day_elem, 'low').text = str(round(low))
        ET.SubElement(day_elem, 'icon').text = _panel_icon(code if code is not None else 3)
    return _xml_response(root)


@app.route('/us/sun/<zipcode>.xml', methods=['POST', 'GET'])
def get_sun_times(zipcode):
    _log_request()
    if (bad := _bad_zip(zipcode)):
        return bad
    # Unlike cur/for, this always returns 200. The panel only emits its weather
    # UI update after parse_sunncs_data runs, and it only fetches sun at
    # startup and then once a day, so a non-200 here would leave the whole
    # screen blank, and sunrise/sunset scenes on old times, for up to a day.
    last_update, sunrise, sunset = '--', None, None
    try:
        data = _cached(zipcode, lambda: _fetch_weather(zipcode))
        last_update = _last_update(datetime.datetime.fromisoformat(data['fetched_at']))
        daily = data['daily']
        today = _local_now(data).date().isoformat()
        if today in daily['time']:
            i = daily['time'].index(today)
            if daily['sunrise'][i] and daily['sunset'][i]:
                sunrise = datetime.datetime.fromisoformat(daily['sunrise'][i])
                sunset = datetime.datetime.fromisoformat(daily['sunset'][i])
    except Exception as e:
        log.error('Sun times fetch failed for %s: %s', zipcode, e)
    if sunrise is None:
        with _lock:
            loc = dict(_zip_cache.get(zipcode) or {})
        if loc.get('timezone'):
            day = _now(loc['timezone']).date()
            times = _solar_times(loc['lat'], loc['lon'], day, loc['timezone'])
            if times:
                sunrise, sunset = times
                log.info('Computed sun times locally for %s', zipcode)
    root = ET.Element('day')
    ET.SubElement(root, 'date').text = last_update
    ET.SubElement(root, 'sunrise').text = _clock(sunrise) if sunrise else '--:--'
    ET.SubElement(root, 'sunset').text = _clock(sunset) if sunset else '--:--'
    # parse() in firmware dispatches to parse_sunncs_data when the raw response
    # contains "city"; the tag name alone satisfies that, and the parser never
    # reads the value. Keep the value constant so no place name can ever contain
    # "current" or "forecast" and divert this to the wrong parser. Must be last
    # so QXmlStreamReader has already parsed date/sunrise/sunset.
    ET.SubElement(root, 'city').text = '-'
    return _xml_response(root)


class SingleWriteHandler(WSGIRequestHandler):
    # The panel's get_data() (0x485bc) calls recv() exactly once, then closes.
    # Werkzeug's default unbuffered writer sends headers and body as separate
    # TCP segments, so the panel only ever saw the headers. Buffer the whole
    # response so it goes out in a single send().
    wbufsize = -1
    # http_check (0x46b24) requires the literal "HTTP/1.1 200 OK". Werkzeug
    # only upgrades the inherited HTTP/1.0 when the server is threaded, so
    # don't rely on that.
    protocol_version = 'HTTP/1.1'


_load_state()

def _stop_on_sigterm(signum, frame):
    # In a container this process is PID 1, and the kernel doesn't apply
    # default signal actions to PID 1, so SIGTERM was ignored: docker stop
    # waited 10 s and Kubernetes 30 s before killing it (exit 137).
    # SystemExit rather than KeyboardInterrupt: Werkzeug only catches
    # KeyboardInterrupt inside serve_forever(), and the socket is already
    # listening before that (during log_startup), so a SIGTERM in that window
    # crashed with an uncaught KeyboardInterrupt. SystemExit(0) exits cleanly
    # at any point and still runs serve_forever()'s server_close(). No logging
    # here: writing to stderr from a signal handler can hit a reentrant write.
    raise SystemExit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    try:
        # The panel firmware only ever connects to 10086; PORT exists for tests.
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10086)),
                request_handler=SingleWriteHandler)
    finally:
        log.info('Shutting down')
        # Request threads are daemon threads and die with the process. Give
        # an in-flight upstream fetch and state save a moment to finish.
        if _lock.acquire(timeout=10):
            _lock.release()
