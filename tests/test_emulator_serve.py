"""emulator/serve.py: fetches weather the way the panel does (one POST, one
recv()) and hands the raw bytes to the page, which runs the firmware's
checks and parsers on them."""
import socket
import threading
import time

import pytest
from werkzeug.serving import make_server

import app as app_module
from conftest import ZIP
from emulator import serve


@pytest.fixture
def weather_server():
    server = make_server('127.0.0.1', 0, app_module.app, threaded=True,
                         request_handler=app_module.SingleWriteHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'127.0.0.1:{server.server_port}'
    server.shutdown()


@pytest.fixture
def emulator(weather_server):
    return serve.create_app(weather_server).test_client()


@pytest.mark.parametrize('endpoint,root', [('cur', '<current>'), ('for', '<forecast>'), ('sun', '<day>')])
def test_panel_fetch_returns_status_line_headers_and_body(weather_server, endpoint, root):
    raw = serve.panel_fetch(weather_server, endpoint, ZIP)
    assert raw.startswith(b'HTTP/1.1 200 OK\r\n')
    assert root.encode() in raw.partition(b'\r\n\r\n')[2]


def test_panel_fetch_only_sees_the_first_segment():
    # A server that sends headers and body separately (Werkzeug's default
    # handler) leaves the panel's single recv() with no body. The emulator
    # must show the same. The pause makes the split certain on loopback.
    listener = socket.create_server(('127.0.0.1', 0))

    def split_server():
        conn, _ = listener.accept()
        with conn:
            conn.recv(1024)
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 19\r\n\r\n')
            time.sleep(0.2)
            conn.sendall(b'<current></current>')

    thread = threading.Thread(target=split_server, daemon=True)
    thread.start()
    try:
        raw = serve.panel_fetch(f'127.0.0.1:{listener.getsockname()[1]}', 'cur', ZIP)
    finally:
        thread.join()
        listener.close()
    assert raw == b'HTTP/1.1 200 OK\r\nContent-Length: 19\r\n\r\n'


def test_route_passes_raw_bytes_through(emulator):
    resp = emulator.get(f'/panel/cur/{ZIP}')
    assert resp.status_code == 200
    assert resp.data.startswith(b'HTTP/1.1 200 OK\r\n')
    assert resp.headers['Cache-Control'] == 'no-store'


def test_route_rejects_unknown_endpoint_and_bad_zip(emulator):
    assert emulator.get(f'/panel/xyz/{ZIP}').status_code == 404
    assert emulator.get('/panel/cur/6060').status_code == 400


def test_route_reports_an_unreachable_server_as_502():
    client = serve.create_app('127.0.0.1:1').test_client()
    resp = client.get(f'/panel/cur/{ZIP}')
    assert resp.status_code == 502


def test_index_page_is_served(emulator):
    resp = emulator.get('/')
    assert resp.status_code == 200
    assert b'<canvas' in resp.data or b'id="panel"' in resp.data


def test_asset_paths_cannot_escape_the_assets_folder(emulator):
    assert emulator.get('/assets/../serve.py').status_code == 404


# --- Testing someone else's server


def test_config_tells_the_page_what_it_can_do(weather_server, tmp_path):
    client = serve.create_app(weather_server, assets_dir=str(tmp_path), allow_override=True).test_client()
    assert client.get('/config').get_json() == {
        'upstream': weather_server, 'override': True, 'assets': False}


def test_page_can_pick_the_server_when_allowed(weather_server):
    client = serve.create_app('127.0.0.1:1', allow_override=True).test_client()
    resp = client.get(f'/panel/cur/{ZIP}?upstream={weather_server}')
    assert resp.data.startswith(b'HTTP/1.1 200 OK\r\n')


def test_page_cannot_pick_the_server_when_open_to_the_lan(weather_server):
    # Otherwise anyone on the network could make this machine connect anywhere.
    client = serve.create_app('127.0.0.1:1', allow_override=False).test_client()
    assert client.get(f'/panel/cur/{ZIP}?upstream={weather_server}').status_code == 403


@pytest.mark.parametrize('bad', ['', 'a b:10086', 'host:0', 'host:65536', 'host:x', 'http://host:10086'])
def test_bad_server_addresses_are_rejected(bad):
    with pytest.raises(ValueError):
        serve.parse_upstream(bad)


@pytest.mark.parametrize('text,parsed', [
    ('example.com', ('example.com', 10086)),
    ('192.168.1.50:8080', ('192.168.1.50', 8080)),
    ('[::1]:10086', ('::1', 10086)),
])
def test_server_addresses_default_to_the_panel_port(text, parsed):
    assert serve.parse_upstream(text) == parsed


def test_route_reports_a_bad_server_address_as_400():
    client = serve.create_app('127.0.0.1:1', allow_override=True).test_client()
    assert client.get(f'/panel/cur/{ZIP}?upstream=a%20b').status_code == 400


def test_runs_without_the_firmware_images(weather_server, tmp_path):
    client = serve.create_app(weather_server, assets_dir=str(tmp_path)).test_client()
    assert client.get('/').status_code == 200
    assert client.get(f'/panel/sun/{ZIP}').status_code == 200
