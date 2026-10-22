"""Quick end-to-end check against a running server.

  set SETUP_MANAGER_TOKEN=devtoken      (before starting the server AND here)
  venv\\Scripts\\python.exe scripts\\smoke_test.py

The server's address is read from the runtime state file, so this works
even when the dashboard had to move to another port. Set
SETUP_MANAGER_URL to point somewhere else.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.server_state import read_state  # noqa: E402

# The server echoes emoji in its task logs; a cp1252 console cannot encode
# them, so force UTF-8 before printing.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _server_url() -> str:
    """Where the running server is listening."""
    override = os.getenv("SETUP_MANAGER_URL")
    if override:
        return override
    state = read_state()
    if state:
        return state.get("url") or f"http://127.0.0.1:{state['port']}"
    return "http://127.0.0.1:8010"


BASE = _server_url()
TOKEN = os.getenv("SETUP_MANAGER_TOKEN")
if not TOKEN:
    sys.exit("Set SETUP_MANAGER_TOKEN to the same value the server was started with.")


def call(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Setup-Token": TOKEN, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


print("health:", call("/health"))
# "echo" is a cmd builtin on Windows and a real program elsewhere, so the
# command has to go through cmd.exe to exist as a program at all.
echo_command = (
    "cmd /c echo Hello from Setup Manager"
    if os.name == "nt"
    else "echo Hello from Setup Manager"
)
task = call("/task/custom", {"command": echo_command, "confirm": True})["task_id"]
for _ in range(20):
    info = call(f"/task/{task}/logs")
    if info["status"] != "running":
        break
    time.sleep(0.5)
print("status:", info["status"])
print("\n".join(info["logs"]))
sys.exit(0 if info["status"] == "completed" else 1)
