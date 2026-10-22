"""Where the Setup Manager server is, and whether it is alive.

The dashboard binds a TCP port, so the port it ends up on cannot always be
the one we would like. This module owns that decision and the runtime
record of it, so the server, the CLI and the smoke test all agree on it.

Only the standard library is used, like the rest of ``core``.
"""
import json
import os
import socket
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: The port the dashboard prefers when it is free.
DEFAULT_PORT = 8010

#: The last port the dashboard may use. The range sits well below the
#: operating system's ephemeral range, so the OS will not hand one of
#: these ports to something else while the server is running.
PORT_RANGE_END = 8099

#: Overrides where the runtime state file is written.
STATE_FILE_ENV = "SETUP_MANAGER_STATE_FILE"

#: Overrides the range of ports the dashboard may use, as "start-end".
PORT_RANGE_ENV = "SETUP_MANAGER_PORT_RANGE"

#: The value the /health endpoint reports, so another program on the same
#: port is not mistaken for us.
HEALTH_MARKER = "setup-manager"

#: How long to wait for a port to answer, when we are checking whether a
#: Setup Manager is already listening there.
PROBE_TIMEOUT = 0.3


def port_range() -> Tuple[int, int]:
    """The range of ports the dashboard may use.

    Defaults to 8010-8099. ``SETUP_MANAGER_PORT_RANGE`` ("9010-9020")
    changes it; a value that makes no sense is ignored.
    """
    raw = (os.getenv(PORT_RANGE_ENV) or "").strip()
    if raw:
        start, _, end = raw.partition("-")
        try:
            first, last = int(start), int(end)
        except ValueError:
            pass
        else:
            if 1 <= first <= last <= 65535:
                return first, last
    return DEFAULT_PORT, PORT_RANGE_END


def state_path() -> Path:
    """The file that records where the running server is."""
    override = os.getenv(STATE_FILE_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "logs" / "server.json"


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """True when nothing is listening on the given local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def find_free_port(
    start: int = DEFAULT_PORT, end: int = PORT_RANGE_END, host: str = "127.0.0.1"
) -> int:
    """The first free port in the range, or RuntimeError if there is none."""
    for port in range(start, end + 1):
        if is_port_free(port, host):
            return port
    raise RuntimeError(f"No free port between {start} and {end}.")


def write_state(port: int, pid: Optional[int] = None, path: Optional[Path] = None) -> Path:
    """Record where the server is, so other tools can find it.

    The API token is deliberately not written: it stays in memory, and the
    browser picks it up from the page itself.
    """
    target = Path(path) if path is not None else state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "port": int(port),
        "pid": int(pid) if pid is not None else os.getpid(),
        "url": f"http://127.0.0.1:{port}",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def read_state(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The recorded state, or None when there is no usable state file."""
    target = Path(path) if path is not None else state_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("port"), int):
        return None
    return data


def clear_state(path: Optional[Path] = None) -> None:
    """Forget the recorded state. Never fails if the file is already gone."""
    target = Path(path) if path is not None else state_path()
    try:
        target.unlink()
    except OSError:
        pass


def is_setup_manager(port: int, host: str = "127.0.0.1", timeout: float = PROBE_TIMEOUT) -> bool:
    """True when a Setup Manager is answering on the given port."""
    # No proxy: a proxy on this machine must not intercept our own API.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{host}:{port}/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return False
    return isinstance(payload, dict) and payload.get("app") == HEALTH_MARKER


def find_running_instance(
    path: Optional[Path] = None,
    scan_from: Optional[int] = None,
    scan_to: Optional[int] = None,
    host: str = "127.0.0.1",
) -> Optional[Dict[str, Any]]:
    """A Setup Manager that is already running, or None.

    The state file is the fast path. The port range is scanned as well, so
    an instance that started before the state file existed is still found.
    """
    if scan_from is None or scan_to is None:
        default_start, default_end = port_range()
        scan_from = default_start if scan_from is None else scan_from
        scan_to = default_end if scan_to is None else scan_to

    state = read_state(path)
    if state:
        port = state["port"]
        if is_setup_manager(port, host):
            return {
                "port": port,
                "url": state.get("url") or f"http://{host}:{port}",
                "pid": state.get("pid"),
            }

    for port in range(scan_from, scan_to + 1):
        if is_port_free(port, host):
            continue
        if is_setup_manager(port, host):
            return {"port": port, "url": f"http://{host}:{port}", "pid": None}
    return None


def choose_port(
    explicit: Optional[int] = None,
    scan_from: Optional[int] = None,
    scan_to: Optional[int] = None,
    host: str = "127.0.0.1",
) -> Tuple[int, Optional[str]]:
    """Decide which port to serve on.

    Returns the port and, when an existing Setup Manager should be reused
    instead of starting a second one, the URL of that instance.
    """
    if scan_from is None or scan_to is None:
        default_start, default_end = port_range()
        scan_from = default_start if scan_from is None else scan_from
        scan_to = default_end if scan_to is None else scan_to

    if explicit is not None:
        port = int(explicit)
        if is_port_free(port, host):
            return port, None
        if is_setup_manager(port, host):
            return port, f"http://{host}:{port}"
        raise RuntimeError(
            f"Port {port} is already in use by another program. "
            f"Free it, or set SETUP_MANAGER_PORT to a different port."
        )

    running = find_running_instance(scan_from=scan_from, scan_to=scan_to, host=host)
    if running:
        return running["port"], running["url"]
    return find_free_port(scan_from, scan_to, host), None
