"""Rules imposed by the panel firmware (TUXW V5.3.21.0), each confirmed by
disassembly. Breaking any of these leaves the panel blank or wrong, usually
with no error anywhere. See docs/PROTOCOL.md for the addresses."""
import re
import socket
import threading
import xml.etree.ElementTree as ET
import datetime

import pytest
from werkzeug.serving import WSGIRequestHandler, make_server

import app as app_module
from conftest import ZIP

ENDPOINTS = ('cur', 'for', 'sun')

# extractIcon() (0x57290) table at 0xd089a8: <icon> value -> image.
FIRMWARE_ICON_TABLE = {
    1: 'sunny', 2: 'mostly_sunny', 3: 'mostly_sunny', 4: 'cloudy', 5: 'sunny',
    6: 'mostly_cloudy', 7: 'cloudy', 8: 'cloudy', 9: None, 10: None, 11: 'fog',
    12: 'storm', 13: 'storm', 14: 'storm', 15: 'thunderstorm', 16: 'thunderstorm',
    17: 'thunderstorm', 18: 'rain', 19: 'chance_of_snow', 20: 'chance_of_snow',
    21: 'chance_of_snow', 22: 'snow', 23: 'chance_of_snow', 24: 'icy', 25: 'sleet',
    26: 'snow', 27: None, 28: None, 29: 'snow', 30: 'sunny', 31: 'snow', 32: 'windy',
    33: 'sunny', 34: 'sunny', 35: 'mostly_cloudy', 36: 'mostly_sunny', 37: 'haze',
    38: 'mostly_cloudy', 39: 'storm', 40: 'storm', 41: 'thunderstorm',
    42: 'thunderstorm', 43: 'chance_of_snow', 44: 'chance_of_snow',
}


def panel_http_check(raw: bytes) -> int:
    # http_check (0x46b24): strstr for "HTTP/1.1 404" first, then requires
    # "HTTP/1.1 200 OK". 0 means the response goes on to parse().
    if b'HTTP/1.1 404' in raw:
        return -404
    if b'HTTP/1.1 200 OK' in raw:
        return 0
    return -405


def panel_dispatch(body: str) -> str:
    # CWeatherHelper::parse() (0x655d4): raw substring search, first match wins.
    if 'current' in body:
        return 'cur'
    if 'forecast' in body:
        return 'for'
    if 'city' in body:
        return 'sun'
    return 'none'


def strtol(s: str) -> int:
    m = re.match(r'\s*([+-]?\d+)', s)
    return int(m.group(1)) if m else 0


def panel_sun_parse(sunrise: str, sunset: str):
    # parse_sunncs_data (0x64c1c): hour = strtol(s), minute = strtol(s + 2).
    # fetch_today_sun_time() (0x40a50) then adds 12 to the sunset hour.
    return (strtol(sunrise), strtol(sunrise[2:])), (strtol(sunset) + 12, strtol(sunset[2:]))


def children(body: str):
    root = ET.fromstring(body)
    return root.tag, [c.tag for c in root]


# --- Wire format: get_data() (0x485bc) does a single recv() into a 2048-byte buffer


@pytest.fixture
def live_server(request):
    """Real Werkzeug server on a random port. Counts the writes that reach the
    socket for each response. Param: (handler class, threaded)."""
    handler_cls, threaded = getattr(request, 'param', (app_module.SingleWriteHandler, True))
    writes = []

    class CountingHandler(handler_cls):
        def setup(self):
            super().setup()
            target = getattr(self.wfile, 'raw', self.wfile)
            original = target.write

            def counted(data):
                writes.append(len(data))
                return original(data)
            target.write = counted

    server = make_server('127.0.0.1', 0, app_module.app, threaded=threaded,
                         request_handler=CountingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, writes
    server.shutdown()


def panel_request(port, endpoint, zipcode=ZIP):
    """Send the request the way the panel does, and return what the panel
    would see: the result of one recv()."""
    with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
        s.sendall(f'POST /us/{endpoint}/{zipcode}.xml HTTP/1.1\r\n'
                  f'Host: weather.tuxconnect.info:10086\r\n\r\n'.encode())
        first_read = s.recv(2048)
        # Drain until the server closes the connection, which it only does
        # after the handler has finished writing. Without this, the write
        # count can be read while the server thread is still mid-response.
        while s.recv(4096):
            pass
        return first_read


SERVER_MODES = [(app_module.SingleWriteHandler, True), (app_module.SingleWriteHandler, False)]


@pytest.mark.parametrize('live_server', SERVER_MODES, indirect=True, ids=['threaded', 'single-thread'])
@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_response_is_one_write_and_fits_one_recv(live_server, endpoint):
    port, writes = live_server
    first_read = panel_request(port, endpoint)
    assert len(writes) == 1, f'response went out in {len(writes)} writes: {writes}'
    assert first_read.startswith(b'HTTP/1.1 200 OK\r\n')
    assert panel_http_check(first_read) == 0
    head, _, body = first_read.partition(b'\r\n\r\n')
    root, _ = children(body.decode())
    assert body.decode().endswith(f'</{root}>'), 'body truncated in the first recv()'
    assert len(first_read) < 2048


@pytest.mark.parametrize('live_server', [(WSGIRequestHandler, True)], indirect=True)
def test_default_werkzeug_handler_splits_response(live_server):
    # Proves the write counting above detects the bug SingleWriteHandler fixes.
    port, writes = live_server
    panel_request(port, 'cur')
    assert len(writes) > 1


@pytest.mark.parametrize('live_server', [(WSGIRequestHandler, False)], indirect=True)
def test_unthreaded_default_handler_fails_http_check(live_server):
    # Proves why SingleWriteHandler pins protocol_version: without it, an
    # unthreaded server answers HTTP/1.0 and the panel rejects everything.
    port, _ = live_server
    assert panel_http_check(panel_request(port, 'cur')) != 0


def test_unavailable_is_rejected_by_http_check(live_server, upstream):
    # 503 must not reach parse(); the panel keeps what it already shows.
    upstream.nbm = upstream.default = 'down'
    port, _ = live_server
    assert panel_http_check(panel_request(port, 'cur')) != 0


def test_bad_zip_is_rejected_by_http_check(live_server):
    port, _ = live_server
    assert panel_http_check(panel_request(port, 'cur', zipcode='abcde')) != 0


# --- XML shape: QXmlStreamReader loops stop at the first unexpected element


def test_current_xml_shape(client):
    body = client.post(f'/us/cur/{ZIP}.xml').data.decode()
    assert children(body) == ('current', ['location', 'temp', 'rhumid', 'wind_dir', 'windspeed', 'icon'])
    assert not body.startswith('<?xml')


def test_forecast_xml_shape(client):
    body = client.post(f'/us/for/{ZIP}.xml').data.decode()
    root = ET.fromstring(body)
    assert root.tag == 'forecast'
    assert [d.tag for d in root] == ['day'] * 4
    for day in root:
        assert [c.tag for c in day] == ['name', 'high', 'low', 'icon']


def test_sun_xml_shape_with_city_last(client):
    body = client.post(f'/us/sun/{ZIP}.xml').data.decode()
    assert children(body) == ('day', ['date', 'sunrise', 'sunset', 'city'])


def test_location_is_city_comma_state(client):
    # DemoWeatherData() uses "Melville, NY".
    root = ET.fromstring(client.post(f'/us/cur/{ZIP}.xml').data)
    assert root.findtext('location') == 'Chicago, IL'


# --- Dispatcher: parse() (0x655d4) picks the parser by substring


@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_each_response_reaches_its_parser(client, endpoint):
    assert panel_dispatch(client.post(f'/us/{endpoint}/{ZIP}.xml').data.decode()) == endpoint


def test_place_name_cannot_divert_sun_response(client, upstream):
    # QString::indexOf is case-sensitive, so only lowercase text can misroute.
    upstream.place_name = 'undercurrent'
    body = client.post(f'/us/sun/{ZIP}.xml').data.decode()
    assert panel_dispatch(body) == 'sun'


def test_sun_reaches_its_parser_with_no_data(client, upstream):
    # Without parse_sunncs_data the panel never refreshes the weather UI.
    upstream.zippopotam = upstream.geocode = 'down'
    r = client.post(f'/us/sun/{ZIP}.xml')
    assert r.status_code == 200
    assert panel_dispatch(r.data.decode()) == 'sun'


# --- Icons: <icon> is an AccuWeather code indexing the firmware table


@pytest.mark.parametrize('wmo', range(100))
def test_every_weather_code_maps_to_a_real_image(wmo):
    code = int(app_module._panel_icon(wmo))
    assert FIRMWARE_ICON_TABLE.get(code), f'WMO {wmo} -> {code} is not a valid image'


@pytest.mark.parametrize('wmo, image', [
    (0, 'sunny'), (2, 'mostly_sunny'), (3, 'cloudy'), (45, 'fog'), (61, 'rain'),
    (66, 'icy'), (75, 'snow'), (80, 'storm'), (95, 'thunderstorm'),
])
def test_icon_semantics(wmo, image):
    assert FIRMWARE_ICON_TABLE[int(app_module._panel_icon(wmo))] == image


# --- Sun times: 12-hour, no leading zero, parsed by strtol


def test_sun_endpoint_times_parse_correctly(client):
    root = ET.fromstring(client.post(f'/us/sun/{ZIP}.xml').data)
    sunrise, sunset = root.findtext('sunrise'), root.findtext('sunset')
    assert (sunrise, sunset) == ('7:27 AM', '6:43 PM')
    assert panel_sun_parse(sunrise, sunset) == ((7, 27), (18, 43))


@pytest.mark.parametrize('hour', range(1, 10))
@pytest.mark.parametrize('minute', [0, 7, 30, 59])
def test_clock_round_trips_through_panel_parser(hour, minute):
    rise = app_module._clock(datetime.datetime(2026, 1, 1, hour, minute))
    sset = app_module._clock(datetime.datetime(2026, 1, 1, hour + 12, minute))
    assert re.fullmatch(r'[1-9]:\d\d AM', rise) and re.fullmatch(r'[1-9]:\d\d PM', sset)
    assert panel_sun_parse(rise, sset) == ((hour, minute), (hour + 12, minute))


def test_clock_midnight_and_noon():
    assert app_module._clock(datetime.datetime(2026, 1, 1, 0, 5)) == '12:05 AM'
    assert app_module._clock(datetime.datetime(2026, 1, 1, 12, 0)) == '12:00 PM'


# --- Sunrise/sunset automations: the scene scheduler uses the parsed times


def panel_sun_trigger(sun_text: str, sunset: bool):
    """The time a sunrise or sunset scene fires, or None if it never does.
    CEventCaptureThread::CompareDateTime (0x305ddc) runs every minute, builds
    the trigger with QTime::setHMS(hour, minute) from fetch_today_sun_time()
    (0x305f74, 0x305fec), and fires when the clock's hour and minute equal it.
    setHMS rejects an out-of-range time, and an invalid QTime never matches."""
    hour, minute = strtol(sun_text) + (12 if sunset else 0), strtol(sun_text[2:])
    return (hour, minute) if 0 <= hour < 24 and 0 <= minute < 60 else None


# Extremes of the lower 48 and Hawaii: earliest sunrise (Caribou), latest
# sunrise and sunset (Ontonagon, western edge of Eastern time), no DST (Phoenix).
SUN_LOCATIONS = {
    'Chicago IL': (41.89, -87.62, 'America/Chicago'),
    'Bellingham WA': (48.75, -122.48, 'America/Los_Angeles'),
    'Caribou ME': (46.86, -68.01, 'America/New_York'),
    'Ontonagon MI': (46.87, -89.31, 'America/Detroit'),
    'Key West FL': (24.56, -81.78, 'America/New_York'),
    'Phoenix AZ': (33.45, -112.07, 'America/Phoenix'),
    'Honolulu HI': (21.31, -157.86, 'Pacific/Honolulu'),
}


def sun_days(lat, lon, tz, year=2026):
    day = datetime.date(year, 1, 1)
    while day.year == year:
        yield day, app_module._solar_times(lat, lon, day, tz)
        day += datetime.timedelta(days=1)


@pytest.mark.parametrize('place', SUN_LOCATIONS)
def test_sun_scenes_fire_at_the_real_time_every_day(place):
    # Every day of the year, DST changes included: what the server sends must
    # make sunrise and sunset scenes fire on the right minute.
    for day, (rise, sset) in sun_days(*SUN_LOCATIONS[place]):
        assert panel_sun_trigger(app_module._clock(rise), sunset=False) == (rise.hour, rise.minute), day
        assert panel_sun_trigger(app_module._clock(sset), sunset=True) == (sset.hour, sset.minute), day


def test_24_hour_sunset_never_fires():
    # The bug the 12-hour format fixed: "18:43" became hour 30, so a sunset
    # scene silently never ran. Proves the check above can fail.
    assert panel_sun_trigger('18:43', sunset=True) is None
    assert panel_sun_trigger('6:43 PM', sunset=True) == (18, 43)


def test_alaska_summer_sun_scenes_fire_on_the_hour():
    # Known firmware limit: a two-digit hour makes strtol(s + 2) read ":42 PM"
    # as minute 0. Anchorage's summer sunset (after 10 PM) and winter sunrise
    # (after 10 AM) still fire, but at the top of the hour.
    off = [(day, sset) for day, times in sun_days(61.22, -149.90, 'America/Anchorage')
           if times and (sset := times[1]).hour >= 22]
    assert off, 'Anchorage has sunsets after 10 PM'
    for day, sset in off:
        assert panel_sun_trigger(app_module._clock(sset), sunset=True) == (sset.hour, 0), day
