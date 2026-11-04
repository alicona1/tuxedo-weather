"""
Local web server for the panel emulator.

The page asks this server for weather, and this server fetches it the way the
panel does: POST /us/<endpoint>/<zip>.xml, then a single recv() of up to 2048
bytes (get_data() at 0x485bc). The raw bytes, status line and headers
included, go back to the page, which runs the firmware's http_check and
parsers on them. So a response the panel would reject is rejected here too.

The server it fetches from is --upstream, e.g. your own weather server. With
no --upstream, this starts app.py in-process on a random local port, using
the same SingleWriteHandler as production. While the emulator only listens on
this machine, the page can also switch servers itself.

The firmware's images make the page look like the panel. Without them
(`python -m emulator.extract_assets` needs the firmware), it draws plain
boxes in the same places, and the response checks work the same.

Usage: python -m emulator.serve [--upstream HOST[:PORT]] [--host 0.0.0.0] [--port 8800]
"""
import argparse
import ipaddress
import os
import re
import socket
import sys
import threading

from flask import Flask, Response, abort, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, 'assets')

ENDPOINTS = ('cur', 'for', 'sun')
ZIP_RE = re.compile(r'\d{5}')
RECV_SIZE = 2048      # the panel's receive buffer
PANEL_TIMEOUT = 10    # get_data()'s socket timeout (timeval at 0x5d2064)
PANEL_PORT = 10086    # the only port the firmware connects to
PANEL_HOST = 'weather.tuxconnect.info:10086'
HOSTNAME_RE = re.compile(r'[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?')
LOOPBACK = ('127.0.0.1', 'localhost', '::1')


def parse_upstream(text: str):
    """'host', 'host:port' or '[v6]:port' -> (host, port). Raises ValueError."""
    m = re.fullmatch(r'\[([0-9A-Fa-f:.]+)\](?::(\d+))?|([^:\[\]]+)(?::(\d+))?', text.strip())
    if not m:
        raise ValueError(f'not a host or host:port: {text!r}')
    if m.group(1):
        host, port = m.group(1), m.group(2)
        ipaddress.IPv6Address(host)
    else:
        host, port = m.group(3), m.group(4)
        if not HOSTNAME_RE.fullmatch(host):
            raise ValueError(f'not a host name or address: {host!r}')
    port = int(port) if port else PANEL_PORT
    if not 0 < port < 65536:
        raise ValueError(f'port out of range: {port}')
    return host, port


def panel_fetch(upstream: str, endpoint: str, zipcode: str) -> bytes:
    """Return what the panel's single recv() would get from `upstream`."""
    with socket.create_connection(parse_upstream(upstream), timeout=PANEL_TIMEOUT) as s:
        s.sendall(f'POST /us/{endpoint}/{zipcode}.xml HTTP/1.1\r\n'
                  f'Host: {PANEL_HOST}\r\n\r\n'.encode())
        return s.recv(RECV_SIZE)


def has_assets(assets_dir: str) -> bool:
    return os.path.isfile(os.path.join(
        assets_dir, 'background', 'LoungeLizard', 'background', 'background.png'))


def create_app(upstream: str, assets_dir: str = ASSETS, allow_override: bool = False) -> Flask:
    """`allow_override` lets the page pick the server per request (?upstream=).
    Only safe while the emulator listens on loopback."""
    emu = Flask(__name__, static_folder=None)

    @emu.route('/')
    def index():
        return send_from_directory(HERE, 'index.html')

    @emu.route('/<name>.<ext>')
    def page_file(name, ext):
        if ext not in ('js', 'css'):
            abort(404)
        return send_from_directory(HERE, f'{name}.{ext}')

    @emu.route('/assets/<path:path>')
    def asset(path):
        # send_from_directory refuses paths that leave assets_dir.
        return send_from_directory(assets_dir, path)

    @emu.route('/config')
    def config():
        return jsonify(upstream=upstream, override=allow_override, assets=has_assets(assets_dir))

    @emu.route('/panel/<endpoint>/<zipcode>')
    def panel(endpoint, zipcode):
        if endpoint not in ENDPOINTS:
            abort(404)
        if not ZIP_RE.fullmatch(zipcode):
            return Response('zip code must be 5 digits', status=400, mimetype='text/plain')
        target = upstream
        if 'upstream' in request.args:
            if not allow_override:
                return Response('the server can only be changed with --upstream while the '
                                'emulator is open to the network', status=403, mimetype='text/plain')
            target = request.args['upstream']
        try:
            parse_upstream(target)
        except ValueError as e:
            return Response(str(e), status=400, mimetype='text/plain')
        try:
            raw = panel_fetch(target, endpoint, zipcode)
        except OSError as e:
            return Response(f'could not reach {target}: {e}', status=502, mimetype='text/plain')
        resp = Response(raw, mimetype='text/plain')
        # The panel decodes the body as Latin-1 (QString::fromAscii).
        resp.charset = 'latin-1'
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    return emu


def _start_local_weather_server() -> str:
    # app.py reads CACHE_DIR at import time; keep its state out of /data.
    os.environ.setdefault('CACHE_DIR', os.path.join(HERE, '.cache'))
    os.makedirs(os.environ['CACHE_DIR'], exist_ok=True)
    from werkzeug.serving import make_server
    import app as weather_app

    server = make_server('127.0.0.1', 0, weather_app.app, threaded=True,
                         request_handler=weather_app.SingleWriteHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f'127.0.0.1:{server.server_port}'


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--upstream', help='HOST[:PORT] of the weather server to test '
                        f'(port defaults to {PANEL_PORT}; default: start app.py in-process)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='address to listen on; 0.0.0.0 opens it to the LAN')
    parser.add_argument('--port', type=int, default=8800)
    args = parser.parse_args()

    if args.upstream:
        try:
            parse_upstream(args.upstream)
        except ValueError as e:
            parser.error(str(e))
    if not has_assets(ASSETS):
        print('No firmware images, so the screens are drawn as plain boxes. '
              'See emulator/README.md to extract them.')
    upstream = args.upstream or _start_local_weather_server()
    local_only = args.host in LOOPBACK
    shown = '<this machine>' if args.host in ('0.0.0.0', '::') else args.host
    print(f'Weather from {upstream}. Emulator at http://{shown}:{args.port}/')
    if not local_only:
        print('Open to the network, so the page cannot change the server; use --upstream.')
    create_app(upstream, allow_override=local_only).run(host=args.host, port=args.port)
    return 0


if __name__ == '__main__':
    sys.exit(main())
