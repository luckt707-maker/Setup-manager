"""Shared test setup for Setup Manager v2.

Puts the project root on ``sys.path`` and points the app at a throwaway
safe root and task database so nothing touches the real machine state.
"""
import json
import os
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: A temporary directory that stands in for C:\\dev during the tests.
SAFE_ROOT = Path(tempfile.mkdtemp(prefix="setup-manager-tests-"))
os.environ["SETUP_MANAGER_SAFE_ROOT"] = str(SAFE_ROOT)
os.environ["SETUP_MANAGER_SETTINGS_FILE"] = str(SAFE_ROOT / "settings-test.json")
os.environ["SETUP_MANAGER_TASKS_DB"] = str(SAFE_ROOT / "tasks-test.db")
# The real watch reads this PC. Tests call the checker with made-up names.
os.environ["SETUP_MANAGER_FILE_WATCH"] = "0"


class _HealthHandler(BaseHTTPRequestHandler):
    """Answers /health the way the Setup Manager API does."""

    def do_GET(self):
        body = json.dumps({"status": "ok", "app": "setup-manager"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_setup_manager():
    """A running server that answers like Setup Manager. Yields its port."""
    server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def busy_port():
    """A port occupied by something that is not Setup Manager."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()
