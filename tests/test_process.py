"""The server as a real process: it must stop promptly on SIGTERM, which is
how both docker stop and Kubernetes end it."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / 'app.py'


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start_server(tmp_path):
    port = free_port()
    env = {**os.environ, 'PORT': str(port), 'CACHE_DIR': str(tmp_path)}
    proc = subprocess.Popen([sys.executable, str(APP)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc, port


def wait_for_port(proc, port):
    deadline = time.monotonic() + 15
    while True:
        try:
            socket.create_connection(('127.0.0.1', port), timeout=1).close()
            return
        except OSError:
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, 'server never started'
            time.sleep(0.001)


def stop(proc):
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    code = proc.wait(timeout=5)
    return code, time.monotonic() - started, proc.stdout.read()


def test_sigterm_stops_running_server_cleanly(tmp_path):
    proc, port = start_server(tmp_path)
    try:
        wait_for_port(proc, port)
        # A full HTTP response means serve_forever() is running.
        with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
            s.sendall(b'GET /health HTTP/1.1\r\nHost: test\r\n\r\n')
            assert s.recv(4096).startswith(b'HTTP/1.1 200 OK')
        code, took, output = stop(proc)
        assert code == 0, output
        assert took < 3
        assert 'Shutting down' in output
        assert 'Traceback' not in output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize('attempt', range(5))
def test_sigterm_during_startup_exits_cleanly(tmp_path, attempt):
    # The socket listens before serve_forever() starts. Sending SIGTERM the
    # moment the port opens lands in that window almost every time; with
    # KeyboardInterrupt it crashed (exit -2) in 39 of 40 tries.
    proc, port = start_server(tmp_path)
    try:
        wait_for_port(proc, port)
        code, _, output = stop(proc)
        assert code == 0, output
        assert 'Traceback' not in output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
