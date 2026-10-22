"""Quick end-to-end check against a running server.

  set SETUP_MANAGER_TOKEN=devtoken      (before starting the server AND here)
  venv\\Scripts\\python.exe scripts\\smoke_test.py

Set SETUP_MANAGER_URL if the server is not on the default port.
"""
import json
import os
import sys
import time
import urllib.request

# The server echoes emoji in its task logs; a cp1252 console cannot encode
# them, so force UTF-8 before printing.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

BASE = os.getenv("SETUP_MANAGER_URL", "http://127.0.0.1:8010")
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
task = call("/task/custom", {"command": "echo Hello from Setup Manager", "confirm": True})["task_id"]
for _ in range(20):
    info = call(f"/task/{task}/logs")
    if info["status"] != "running":
        break
    time.sleep(0.5)
print("status:", info["status"])
print("\n".join(info["logs"]))
sys.exit(0 if info["status"] == "completed" else 1)
