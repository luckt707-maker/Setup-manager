def _ensure_setup_manager_imports() -> None:
    """Install requirements.txt when this interpreter cannot import the app.

    The dashboard imports FastAPI at startup. A missing package used to end
    that startup with ModuleNotFoundError. The install uses this interpreter,
    which is the project virtual environment when the launcher started it.
    """
    import importlib.util
    import subprocess
    import sys
    from pathlib import Path

    modules = ("fastapi", "uvicorn", "pydantic", "websockets")
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if not missing:
        return
    requirements = Path(__file__).resolve().parent / "requirements.txt"
    print(
        "Setup Manager is missing " + ", ".join(missing) + ". Installing from " + str(requirements),
        flush=True,
    )
    command = [sys.executable, "-m", "pip", "install", "-r", str(requirements)]
    if sys.prefix == sys.base_prefix:
        command.insert(4, "--user")
    completed = subprocess.run(command)
    if completed.returncode != 0:
        raise SystemExit("Setup Manager could not install its Python packages.")
    import site
    if sys.prefix == sys.base_prefix:
        user_site = site.getusersitepackages()
        if isinstance(user_site, str) and user_site not in sys.path:
            site.addsitedir(user_site)
    importlib.invalidate_caches()
    still = [name for name in modules if importlib.util.find_spec(name) is None]
    if still:
        raise SystemExit("Setup Manager still cannot import: " + ", ".join(still))


_ensure_setup_manager_imports()

import asyncio
import base64
from html import escape as html_escape
import inspect
import ipaddress
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import socket
import webbrowser
import platform
import re
import secrets
from urllib.parse import quote_plus
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import (
    BackgroundTasks,
    Body,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import (
    CommandExecutor,
    ProjectDetector,
    ProjectOrchestrator,
    SecurityScanner,
    Setup,
)
from core.command_risk import command_risk, refusal
from core.contained import read_text_inside
from core.dependencies import declared_requirements, find_venv, normalize_name
from core.privacy import check_this_pc
from core.file_watch import WatchDisabled, runner as file_watch_runner
from core.setup import source_retry_command
from core.settings import allowed_project_roots, projects_root, settings_manager
from core.where import find_programs
from core.tooling import gradle_command, make_command
from core.server_state import (
    HEALTH_MARKER,
    choose_port,
    clear_state,
    port_range,
    write_state,
)
from doctor import (
    BuildDoctor,
    DoctorLogManager,
    KnowledgeBase,
    available_repairs,
    check_project_dependencies,
    diagnose_build_output,
    run_machine_diagnostics,
    summarize,
)
from core.guide import advise, apply_model_reply, present
from core.project_check import check_project, download_safe_updates
from ollama_client import OllamaClient, OllamaError, untrusted_block

# --------------------------------------------------------------------------- #
#  App / logging
# --------------------------------------------------------------------------- #
logger = logging.getLogger("setup_manager.api")
LOG_LEVEL = os.getenv("SETUP_MANAGER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(levelname)s:%(name)s:%(message)s")

from logging.handlers import RotatingFileHandler  # noqa: E402

# FIX 1: Added missing opening parenthesis to Path(__file__)
LOG_DIR = Path(__file__).with_name("logs")
LOG_DIR.mkdir(exist_ok=True)
_file_handler = RotatingFileHandler(LOG_DIR / "server.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_file_handler)

APP_HOST = os.getenv("SETUP_MANAGER_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("SETUP_MANAGER_PORT", "8010"))

# Ollama / Qwen assistant tuning (see ollama_client.py for env defaults)
AI_TEMPERATURE = float(os.getenv("SETUP_MANAGER_AI_TEMPERATURE", "0.2"))
AI_MAX_TOKENS = int(os.getenv("SETUP_MANAGER_AI_MAX_TOKENS", "1024"))

APP_VERSION = "1.2.0"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Record where the server is listening, for as long as it runs.

    The state file is how the CLI, the shortcut and the smoke test find
    the server when it could not use the preferred port.
    """
    try:
        write_state(APP_PORT)
    except OSError as exc:
        logger.warning("Could not record the server state: %s", exc)
    # Check the AI in the background: a missing Ollama must not slow or stop startup.
    threading.Thread(target=_log_ai_health, daemon=True).start()
    # The file watch is off until the user starts it from the dashboard.
    yield
    file_watch_runner.stop("Setup Manager closed.")
    clear_state()


def _log_ai_health() -> None:
    """Say once, at startup, whether the local AI is ready."""
    try:
        status = OllamaClient().status()
    except Exception as exc:  # noqa: BLE001 - never crash because of the AI
        logger.warning("AI check failed: %s", exc)
        return
    if status.get("state") == "ready":
        logger.info("AI ready: %s at %s", status.get("model"), status.get("base_url"))
    else:
        # Ollama being offline is common, not an emergency.
        logger.info("%s", status.get("message"))


app = FastAPI(
    title="Setup Manager Web API",
    version=APP_VERSION,
    redirect_slashes=False,
    lifespan=_lifespan,
)

# --------------------------------------------------------------------------- #
#  Security layer
#  * Host allow-list           -> blocks DNS-rebinding attacks on localhost
#  * Per-session API token     -> blocks drive-by requests from other websites
#  * Loopback peer check       -> only this machine may connect
#  * Origin check              -> blocks cross-site browser requests / websockets
#  * Security headers + CSP
# --------------------------------------------------------------------------- #
API_TOKEN = os.getenv("SETUP_MANAGER_TOKEN") or secrets.token_urlsafe(32)
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
PUBLIC_PATHS = {"/", "/health", "/favicon.ico"}
MAX_CONCURRENT_TASKS = int(os.getenv("SETUP_MANAGER_MAX_TASKS", "4"))
MAX_LOG_LINES = int(os.getenv("SETUP_MANAGER_MAX_LOG_LINES", "20000"))
ALLOW_SHELL = os.getenv("SETUP_MANAGER_ALLOW_SHELL", "0").lower() in {"1", "true", "yes"}
NOTIFY = os.getenv("SETUP_MANAGER_NOTIFY", "0").lower() in {"1", "true", "yes"}

app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


def _token_ok(candidate: Optional[str]) -> bool:
    """Check a token without crashing on unusual characters.

    Header values arrive as latin-1 decoded text, so a token can hold
    characters that str-based compare_digest refuses to compare.
    """
    if not candidate:
        return False
    return secrets.compare_digest(candidate.encode("utf-8"), API_TOKEN.encode("utf-8"))


def _loopback_peer(client) -> bool:
    """Only this machine may talk to the server.

    The Host header is chosen by the caller, so only the socket peer address
    shows where a request really came from.
    """
    try:
        return bool(client) and ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _origin_ok(origin: Optional[str], host: Optional[str]) -> bool:
    if not origin:
        return True  # non-browser clients (curl, tests) send no Origin
    return origin in {f"http://{host}", f"https://{host}"}


# Never authenticate by Origin: unrelated webviews can send a vscode-webview
# origin too. An extension must send the same X-Setup-Token as every client.
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if not _loopback_peer(request.client):
        logger.warning("BLOCKED Non-loopback peer: %s", getattr(request.client, "host", None))
        return JSONResponse({"detail": "Loopback clients only"}, status_code=403)
    path = request.url.path
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")

    if not _origin_ok(origin, host):
        logger.warning(f"BLOCKED Cross-origin request: Origin={origin}, Host={host}, Path={path}")
        return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)

    is_public = path in PUBLIC_PATHS or path.startswith("/static/")
    has_token = _token_ok(request.headers.get("x-setup-token"))

    if not is_public and not has_token:
        logger.warning(f"BLOCKED Missing token: Path={path}")
        return JSONResponse({"detail": "Missing or invalid API token"}, status_code=401)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; script-src-attr 'none'; "
        "connect-src 'self' ws://127.0.0.1:* ws://localhost:* https://pypi.org https://npmjs.com https://docs.microsoft.com; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if not path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response

# --------------------------------------------------------------------------- #
#  State
# --------------------------------------------------------------------------- #
_db_local = threading.local()

# FIX 2: Fixed Path(__file__) syntax and removed extra closing parenthesis
TASK_DB_PATH = Path(
    os.getenv("SETUP_MANAGER_TASKS_DB", str(Path(__file__).with_name("tasks.db")))
).expanduser().resolve()
TASK_TTL_SECONDS = 3600  # completed tasks are purged after 1h


def get_db() -> sqlite3.Connection:
    if not hasattr(_db_local, "connection"):
        connection = sqlite3.connect(
            str(TASK_DB_PATH), timeout=30
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        _db_local.connection = connection
    return _db_local.connection


def init_db() -> None:
    db = get_db()
    db.execute("PRAGMA journal_mode = WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at REAL,
            logs TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
    if "result_json" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'")
    if "label" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    # One row per log line: O(1) appends instead of rewriting a growing JSON blob.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS task_logs (
            task_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            line TEXT NOT NULL,
            PRIMARY KEY (task_id, seq)
        )
        """
    )
    # Tasks left "running" by a crash/restart can never finish: mark them failed.
    db.execute(
        "UPDATE tasks SET status = 'failed', finished_at = ?, updated_at = ? WHERE status = 'running'",
        (time.time(), _now_iso()),
    )
    db.commit()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cleanup_tasks() -> None:
    """Purge completed/failed tasks older than 1 hour."""
    db = get_db()
    cutoff = time.time() - TASK_TTL_SECONDS
    db.execute(
        "DELETE FROM task_logs WHERE task_id IN (SELECT task_id FROM tasks "
        "WHERE status IN ('completed', 'failed', 'cancelled') AND finished_at < ?)",
        (cutoff,),
    )
    db.execute(
        "DELETE FROM tasks WHERE status IN ('completed', 'failed', 'cancelled') AND finished_at < ?",
        (cutoff,),
    )
    db.commit()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _new_task(label: str = "") -> str:
    _cleanup_tasks()
    db = get_db()
    running = db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
    if running >= MAX_CONCURRENT_TASKS:
        raise HTTPException(429, f"Too many tasks running ({running}). Wait for one to finish.")
    task_id = str(uuid.uuid4())
    now = _now_iso()
    db.execute(
        "INSERT INTO tasks (task_id, status, created_at, updated_at, finished_at, logs, label) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, "running", now, now, None, "[]", label[:200]),
    )
    db.commit()
    return task_id


def _finish_task(task_id: str, status: str, **extra: Any) -> None:
    db = get_db()
    now = _now_iso()
    finished_at = time.time()

    if not db.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone():
        return
    with _cancel_lock:
        event = CANCEL_EVENTS.pop(task_id, None)
    # A task the user cancelled did not complete, whatever it reported.
    if event is not None and event.is_set() and status in {"completed", "failed"}:
        status = "cancelled"
    if extra:
        get_log_callback(task_id)(f"[SYSTEM] Task finished with status: {status}. Extra: {extra}")
    db.execute(
        "UPDATE tasks SET status = ?, updated_at = ?, finished_at = ?, result_json = ? WHERE task_id = ?",
        (status, now, finished_at, json.dumps(extra), task_id),
    )
    db.commit()

    # Trigger desktop notification on completion
    if status == "completed":
        _send_notification("Setup Manager", f"✅ Task {task_id[:8]} completed successfully!")
    elif status == "failed":
        _send_notification("Setup Manager", f"❌ Task {task_id[:8]} failed.")


CANCEL_EVENTS: Dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()


def _cancel_event(task_id: str) -> threading.Event:
    with _cancel_lock:
        return CANCEL_EVENTS.setdefault(task_id, threading.Event())


class _TaskLogger:
    """Log callback that also carries the task's cancel event for CommandExecutor."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.cancel_event = _cancel_event(task_id)
        self._progress = 0.0

    def __call__(self, message: str) -> None:
        # Detect progress patterns like "[3/10]" or "30%" in log messages
        import re as _re
        progress_match = _re.search(r'\[(\d+)/(\d+)\]', message)
        if progress_match:
            current = int(progress_match.group(1))
            total = int(progress_match.group(2))
            if total > 0:
                self._progress = min(100.0, (current / total) * 100.0)
        percent_match = _re.search(r'(\d+)%', message)
        if percent_match:
            self._progress = min(100.0, float(percent_match.group(1)))
        _append_log(self.task_id, message, self._progress if self._progress > 0 else None)

    def set_progress(self, value: float) -> None:
        """Explicitly set progress (0-100)."""
        self._progress = max(0.0, min(100.0, value))


def get_log_callback(task_id: str) -> _TaskLogger:
    return _TaskLogger(task_id)


_log_lock = threading.Lock()


def _append_log(task_id: str, message: str, progress: Optional[float] = None) -> None:
    db = get_db()
    # A cancel and the commandhave output are written from different threads,
    # so the sequence number is handed out under a lock and computed inside
    # the INSERT: two writers can never pick the same one.
    with _log_lock:
        row = db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM task_logs WHERE task_id = ?", (task_id,)
        ).fetchone()
        seq = row["n"]
        if seq >= MAX_LOG_LINES:
            if seq == MAX_LOG_LINES:
                db.execute("INSERT INTO task_logs VALUES (?, ?, ?)", (task_id, seq, "[log truncated: line limit reached]"))
                db.commit()
            return
        db.execute(
            "INSERT INTO task_logs (task_id, seq, line) "
            "SELECT ?, COALESCE(MAX(seq), -1) + 1, ? FROM task_logs WHERE task_id = ?",
            (task_id, str(message)[:4000], task_id),
        )
        # Update progress if provided
        if progress is not None:
            db.execute("UPDATE tasks SET result_json = json_set(COALESCE(result_json, '{}'), '$.progress', ?) WHERE task_id = ?", 
                       (progress, task_id))
        db.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (_now_iso(), task_id))
        db.commit()


def _get_task(task_id: str) -> Optional[Dict[str, Any]]:
    row = get_db().execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    task = dict(row)
    task.pop("logs", None)
    task["logs"] = _get_logs(task_id)
    task.update(json.loads(task.pop("result_json") or "{}"))
    return task


def _get_logs(task_id: str, after: int = -1) -> List[str]:
    rows = get_db().execute(
        "SELECT line FROM task_logs WHERE task_id = ? AND seq > ? ORDER BY seq", (task_id, after)
    ).fetchall()
    return [r["line"] for r in rows]


init_db()


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _safe_root() -> Path:
    """Main projects folder. Explicit and fail-closed (no cwd heuristics)."""
    return projects_root()


def _validate_safe_path(path: str) -> str:
    """Resolve symlinks/junctions/.. and require the result to be inside an allowed root."""
    if not path or not path.strip():
        raise HTTPException(400, "A project path is required")
    if "\x00" in path:
        raise HTTPException(400, "Invalid path")
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        raise HTTPException(400, "Invalid path")
    roots = allowed_project_roots()
    if not any(resolved.is_relative_to(root) for root in roots):
        shown = ", ".join(str(root) for root in roots)
        raise HTTPException(400, f"Security Error: path is outside the allowed root {shown}")
    return str(resolved)


def _allowed_file_roots() -> List[Path]:
    """Where a file the user picked may come from.

    A downloaded file lands in the Downloads folder, not in the projects
    root, so the download folders are allowed next to the safe root.
    """
    home = Path.home()
    return [*allowed_project_roots(), home / "Downloads", home / "Desktop"]


def _validate_file_path(path: str) -> Path:
    """A single file the user chose: it must exist and be somewhere allowed."""
    if not path or not path.strip():
        raise HTTPException(400, "A file path is required")
    if "\x00" in path:
        raise HTTPException(400, "Invalid path")
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        raise HTTPException(400, "Invalid path")
    for root in _allowed_file_roots():
        if resolved.is_relative_to(root):
            try:
                is_file = resolved.is_file()
            except OSError:
                raise HTTPException(400, "Invalid path")
            if not is_file:
                raise HTTPException(400, "Choose an existing file")
            return resolved
    raise HTTPException(
        400,
        "Security Error: the file is outside the allowed roots "
        f"({', '.join(str(root) for root in _allowed_file_roots())})",
    )


def _validate_path(path: str) -> str:
    # 1. Security Check: Path Traversal
    path = _validate_safe_path(path)

    # 2. Existence Check
    p = Path(path).expanduser().resolve()
    try:
        exists = p.exists()
        is_dir = p.is_dir() if exists else False
    except OSError:
        raise HTTPException(400, "Invalid path")
    if not exists:
        raise HTTPException(400, f"Path does not exist: {p}")
    if not is_dir:
        raise HTTPException(400, f"Path is not a directory: {p}")
    return str(p)


def _send_notification(title: str, message: str) -> None:
    """Best-effort desktop toast. Opt-in (SETUP_MANAGER_NOTIFY=1); never blocks a task."""
    if not NOTIFY:
        return

    def _notify() -> None:
        try:
            system = platform.system()
            if system == "Windows":
                # SECURITY FIX: Use base64 encoding to prevent PowerShell injection
                # via user-controlled title/message strings
                encoded_title = base64.b64encode(title.encode("utf-8")).decode("ascii")
                encoded_message = base64.b64encode(message.encode("utf-8")).decode("ascii")
                script = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "$n = New-Object System.Windows.Forms.NotifyIcon; "
                    "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
                    "$t = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded_title + "')); "
                    "$m = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded_message + "')); "
                    "$n.ShowBalloonTip(5000, $t, $m, 'Info'); Start-Sleep -Seconds 6; $n.Dispose()"
                )
                subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif system == "Linux":
                subprocess.Popen(["notify-send", title, message])
            elif system == "Darwin":
                # SECURITY FIX: Use base64 encoding to prevent osascript injection
                encoded_title = base64.b64encode(title.encode("utf-8")).decode("ascii")
                encoded_message = base64.b64encode(message.encode("utf-8")).decode("ascii")
                script = (
                    'set t to (do shell script "echo ' + encoded_title + ' | base64 -D"); '
                    'set m to (do shell script "echo ' + encoded_message + ' | base64 -D"); '
                    'display notification m with title t'
                )
                subprocess.Popen(["osascript", "-e", script])
        except Exception as exc:  # notifications must never break a task
            logger.warning("Notification failed: %s", exc)

    threading.Thread(target=_notify, daemon=True).start()


def _scan_with_defender(path: Path) -> Dict[str, Any]:
    """Run a custom Microsoft Defender scan when active protection is available."""
    if sys.platform != "win32":
        return {"status": "unavailable", "message": "Microsoft Defender scanning is available only on Windows."}

    encoded_path = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
$target = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
if (-not (Get-Command Get-MpComputerStatus -ErrorAction SilentlyContinue) -or
    -not (Get-Command Start-MpScan -ErrorAction SilentlyContinue)) {{
    [pscustomobject]@{{ status = 'unavailable'; message = 'Microsoft Defender scan commands are unavailable.' }} | ConvertTo-Json -Compress
    exit 0
}}
try {{
    $status = Get-MpComputerStatus -ErrorAction Stop
    if (-not $status.AntivirusEnabled) {{
        [pscustomobject]@{{ status = 'unavailable'; message = 'Microsoft Defender antivirus is disabled.' }} | ConvertTo-Json -Compress
        exit 0
    }}
    $scanStart = (Get-Date).AddSeconds(-5)
    Start-MpScan -ScanType CustomScan -ScanPath $target -ErrorAction Stop
    $escapedTarget = [regex]::Escape($target)
    $detections = @(Get-MpThreatDetection -ErrorAction Stop | Where-Object {{
        $_.InitialDetectionTime -ge $scanStart -and ($_.Resources -join "`n") -match $escapedTarget
    }} | ForEach-Object {{
        [pscustomobject]@{{ threat_id = $_.ThreatID; resources = @($_.Resources); detected = $_.InitialDetectionTime }}
    }})
    $scanStatus = if ($detections.Count -gt 0) {{ 'threats' }} else {{ 'clean' }}
    [pscustomobject]@{{ status = $scanStatus; detections = $detections; message = if ($detections.Count -gt 0) {{ 'Threats were detected in the scanned path.' }} else {{ 'Microsoft Defender completed the custom scan with no matching detections.' }} }} | ConvertTo-Json -Depth 5 -Compress
}} catch {{
    [pscustomobject]@{{ status = 'error'; message = $_.Exception.Message }} | ConvertTo-Json -Compress
}}
"""
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
        base64.b64encode(script.encode("utf-16le")).decode("ascii"),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "message": f"Microsoft Defender scan could not complete: {exc}"}
    if result.returncode != 0:
        return {"status": "error", "message": result.stderr.strip() or "Microsoft Defender scan failed."}
    try:
        report = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, TypeError):
        return {"status": "error", "message": result.stderr.strip() or "Microsoft Defender returned an unreadable scan result."}
    return report


def _enforce_defender_result(report: Dict[str, Any], path: Path) -> None:
    status = report.get("status")
    if status == "threats":
        raise HTTPException(400, f"Microsoft Defender detected a threat in {path}; the operation was blocked.")
    if status == "error":
        raise HTTPException(503, f"Could not verify {path} with Microsoft Defender: {report.get('message', 'scan failed')}")
    if status not in {"clean", "unavailable"}:
        raise HTTPException(503, f"Microsoft Defender returned an unknown result for {path}; the operation was blocked.")
    if status == "unavailable":
        logger.warning("Defender scan unavailable for %s: %s", path, report.get("message", "unknown reason"))
        if os.getenv("SETUP_MANAGER_REQUIRE_DEFENDER", "0").lower() in {"1", "true", "yes"}:
            raise HTTPException(503, f"Microsoft Defender is required but unavailable: {report.get('message', 'unknown reason')}")


def _call_with_optional_callback(
    fn: Callable, *args: Any, callback: Optional[Callable] = None, **kwargs: Any
) -> Any:
    """Pass ``callback`` only if the target function actually accepts it."""
    try:
        sig = inspect.signature(fn)
        if callback is not None and "callback" in sig.parameters:
            return fn(*args, callback=callback, **kwargs)
    except (TypeError, ValueError):
        pass
    return fn(*args, **kwargs)


def _missing_python_test_packages(python: str, project: Path) -> List[str]:
    """Runtime and development packages that are declared and not installed yet.

    Running the tests needs both. A fresh project environment can import the
    program only after the runtime packages are present, and the test files
    import the development packages on top of that.
    """
    wanted = [("pytest", "pytest")]
    try:
        declared = declared_requirements(project)
    except Exception:
        declared = None
    if declared is not None:
        for requirement in declared.requirements:
            wanted.append((requirement.name, requirement.to_pip()))
        for item in declared.skipped:
            if "development dependency" in item.reason and item.name.strip().lower() != "python":
                wanted.append((item.name, item.name))
    seen = set()
    unique: List[tuple] = []
    for name, spec in wanted:
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        unique.append((name, spec))
    listed = subprocess.run(
        [python, "-m", "pip", "list", "--format=json"],
        cwd=str(project),
        capture_output=True,
        text=True,
    )
    installed: set = set()
    if listed.returncode == 0 and listed.stdout.strip():
        try:
            installed = {normalize_name(item["name"]) for item in json.loads(listed.stdout)}
        except (json.JSONDecodeError, KeyError, TypeError):
            installed = set()
    return [spec for name, spec in unique if normalize_name(name) not in installed]


def _project_python(project: Path) -> Optional[Path]:
    """The target project's own interpreter (never the Setup Manager's)."""
    for name in ("venv", ".venv"):
        candidate = project / name / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
#  Models
# --------------------------------------------------------------------------- #
class AnalyzeRequest(BaseModel):
    path: str = Field("", max_length=4096)


class MountRequest(BaseModel):
    image_path: str = Field(..., min_length=1, max_length=1024)


class FileScanRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)


class LaunchRequest(BaseModel):
    path: str
    candidate_index: int = Field(..., ge=0)
    run_mode: Literal["host", "docker"] = "host"


class CustomCommandRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=2000)
    cwd: Optional[str] = None
    confirm: bool = False


class PathRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)


class ExtensionRequest(BaseModel):
    project_type: str
    ext_types: Optional[List[str]] = None


class MakefileRequest(BaseModel):
    action: str = Field(..., pattern="^(template|url)$")
    url: Optional[str] = None


class AIMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1, max_length=16000)


class AIChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=16000)
    path: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(None, ge=1, le=8192)
    include_context: bool = True
    history: List[AIMessage] = Field(default_factory=list)


class GenerateSetupRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)


# --------------------------------------------------------------------------- #
#  System Health
# --------------------------------------------------------------------------- #
@app.get("/system/doctor")
def system_doctor(path: str = "") -> Dict[str, Any]:
    """Check tools this project needs. A missing unused tool is not an error.

    With no project selected, installed tools are reported and anything
    absent is left as not required, because nothing has asked for it.
    """
    from doctor.diagnostics import tooling_report

    project = Path(_validate_path(path)) if path.strip() else None
    return tooling_report(project)


# --------------------------------------------------------------------------- #
#  Online Search — find packages and docs from safe sources
# --------------------------------------------------------------------------- #
SAFE_SEARCH_SOURCES = {
    "pypi": "https://pypi.org/search/?q={query}",
    "npm": "https://www.npmjs.com/search?q={query}",
    "crates": "https://crates.io/search?q={query}",
    "docs": "https://www.google.com/search?q={query}+site:docs.python.org+OR+site:nodejs.org+OR+site:docs.microsoft.com",
}


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    source: str = Field("auto", pattern="^(auto|pypi|npm|crates|docs)$")


@app.post("/search")
async def search_online(request: SearchRequest) -> Dict[str, Any]:
    """Search for packages or documentation from safe, trusted sources.

    This endpoint returns search URLs and, when possible, direct package
    metadata. It never executes arbitrary code or fetches untrusted content.
    """
    query = request.query.strip()
    if not query:
        raise HTTPException(400, "Search query is required")

    results: Dict[str, Any] = {"query": query, "sources": {}}

    # Determine which sources to search
    if request.source == "auto":
        sources = list(SAFE_SEARCH_SOURCES.keys())
    else:
        sources = [request.source]

    for src in sources:
        template = SAFE_SEARCH_SOURCES.get(src)
        if not template:
            continue
        url = template.format(query=quote_plus(query))
        results["sources"][src] = {"url": url}

        # For package registries, try to fetch direct metadata
        if src == "pypi":
            try:
                import urllib.request as urllib_req
                import urllib.error as urllib_err
                api_url = f"https://pypi.org/pypi/{query}/json"
                req = urllib_req.Request(api_url, headers={"User-Agent": "SetupManager/1.0"})
                with urllib_req.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    info = data.get("info", {})
                    results["sources"][src]["package"] = {
                        "name": info.get("name"),
                        "version": info.get("version"),
                        "summary": info.get("summary"),
                        "homepage": info.get("home_page"),
                    }
            except (urllib_err.URLError, json.JSONDecodeError, TimeoutError, Exception):
                pass  # Return just the search URL if direct lookup fails

        elif src == "npm":
            try:
                import urllib.request as urllib_req
                import urllib.error as urllib_err
                api_url = f"https://registry.npmjs.org/{query}"
                req = urllib_req.Request(api_url, headers={"User-Agent": "SetupManager/1.0"})
                with urllib_req.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    latest = data.get("dist-tags", {}).get("latest")
                    results["sources"][src]["package"] = {
                        "name": data.get("name"),
                        "version": latest,
                        "description": data.get("description"),
                        "homepage": data.get("homepage"),
                    }
            except (urllib_err.URLError, json.JSONDecodeError, TimeoutError, Exception):
                pass

    return results


# --------------------------------------------------------------------------- #
#  Doctor (full diagnostics, build-failure analysis, repairs)
# --------------------------------------------------------------------------- #
@app.post("/doctor/run")
async def doctor_run(request: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """Run a doctor pass over the machine, and optionally over a project folder.

    Body: {"path": "<project folder, optional>"}.
    Paths are validated against the safe root, exactly like the other endpoints.
    This endpoint never runs commands; diagnosing a build that failed is a
    separate call to /doctor/diagnose, which only matches text.
    """
    raw_path = (request.get("path") or "").strip()
    project = _validate_path(raw_path) if raw_path else None

    doctor = BuildDoctor(Path(project) if project else None)
    result = await asyncio.to_thread(doctor.run)
    report = doctor.write_report(result)
    if report:
        result["report"] = str(report)
    return result


@app.post("/doctor/dependencies")
async def doctor_dependencies(request: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """Report the dependencies and files a project is missing.

    Body: {"path": "<project folder>"}. The folder is only read: nothing is
    installed and nothing is run. The report says which packages the project
    declares but does not have, which modules it imports that nothing
    provides, which imports point at a file that is not there, and which
    Node packages are missing.
    """
    raw_path = (request.get("path") or "").strip()
    if not raw_path:
        raise HTTPException(400, "A project path is required")
    path = _validate_path(raw_path)
    findings = await asyncio.to_thread(check_project_dependencies, Path(path))
    return {
        "path": path,
        "findings": [finding.to_dict() for finding in findings],
        "summary": summarize(findings),
    }


@app.post("/doctor/diagnose")
async def doctor_diagnose(request: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """Match captured build output against the known-failure table.

    Body: {"output": "<the text the build printed>"}. This only inspects the
    text it is given; nothing is executed.
    """
    output = request.get("output") or ""
    if not isinstance(output, str):
        raise HTTPException(400, "output must be a string")
    findings = diagnose_build_output(output)
    return {
        "matched": [f.to_dict() for f in findings if f.id.startswith("build.")],
        "summary": summarize(findings),
    }


@app.get("/doctor/history")
async def doctor_history(limit: int = Query(20, ge=1, le=200)) -> Dict[str, Any]:
    """Recent doctor runs."""
    manager = DoctorLogManager()
    return {"runs": await asyncio.to_thread(manager.history, limit)}


@app.get("/doctor/knowledge")
async def doctor_knowledge() -> Dict[str, Any]:
    """The known-failure table the doctor matches build output against."""
    return {"failures": KnowledgeBase.all_failures()}


# --------------------------------------------------------------------------- #
#  Dependency Fix — diagnose and repair missing dependencies
# --------------------------------------------------------------------------- #

class DepFixRequest(BaseModel):
    path: str = Field(..., min_length=1)


@app.post("/dependencies/diagnose")
async def dependencies_diagnose(request: DepFixRequest) -> Dict[str, Any]:
    """Diagnose missing dependencies and return actionable fix items.

    Returns a list of problems with the information needed to fix them,
    as well as a summary of what can be auto-fixed vs what needs manual
    intervention.
    """
    path = _validate_path(request.path)
    project = Path(path)

    # Run dependency check
    findings = await asyncio.to_thread(check_project_dependencies, project)

    # Classify findings into fixable categories
    python_missing = []
    node_missing = []
    left_out = []
    runtime_names = set()
    venv_missing = False
    other_problems = []

    for f in findings:
        d = f.to_dict()
        fid = d.get("id", "")
        if fid == "dependencies.not_installed":
            python_missing = d.get("data", {}).get("packages", [])
            runtime_names = {normalize_name(name) for name in python_missing}
        elif fid == "node.not_installed":
            node_missing = d.get("data", {}).get("packages", [])
        elif fid == "dependencies.venv_missing":
            venv_missing = True
        elif fid == "dependencies.not_needed":
            left_out = d.get("data", {}).get("packages", [])
        elif fid == "dependencies.import_missing":
            # Already listed above when the package is a declared runtime dependency.
            modules = d.get("data", {}).get("modules", [])
            packages = d.get("data", {}).get("packages", [])
            for m, p in zip(modules, packages):
                if normalize_name(m) in runtime_names or normalize_name(p) in runtime_names:
                    continue
                other_problems.append({
                    "type": "undeclared_import",
                    "module": m,
                    "package": p,
                    "title": f"Import '{m}' has no installed package",
                    "detail": (
                        f"The project imports '{m}' (package: {p}), "
                        "and that package is not declared."
                    ),
                    "fixable": False,
                    "search_url": f"https://pypi.org/search/?q={quote_plus(p)}",
                })
        elif d.get("status") in ("fail", "warn"):
            other_problems.append({
                "type": "other",
                "title": d.get("title", ""),
                "detail": d.get("detail", ""),
                "fix": d.get("fix", ""),
                "fixable": False,
            })

    can_auto_fix = bool(python_missing or node_missing)
    return {
        "path": path,
        "python_missing": python_missing,
        "node_missing": node_missing,
        "venv_missing": venv_missing,
        "other_problems": other_problems,
        "left_out": left_out,
        "can_auto_fix": can_auto_fix,
        "summary": _dependency_summary(findings),
    }


def _dependency_summary(findings) -> str:
    """One sentence for the dependency screen. The counts stay in the doctor API."""
    counts = summarize(findings)
    fail = counts.get("fail", 0)
    warn = counts.get("warn", 0)
    if not fail and not warn:
        return "Nothing is missing for this program to run."
    parts = []
    if fail:
        parts.append(f"{fail} problem" + ("" if fail == 1 else "s"))
    if warn:
        parts.append(f"{warn} note" + ("" if warn == 1 else "s"))
    return ", ".join(parts) + "."


@app.post("/dependencies/fix")
async def dependencies_fix(
    request: DepFixRequest,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """Fix missing dependencies by installing them.

    Installs missing Python packages (pip) and Node.js packages (npm).
    If the project has no virtual environment, creates one first.
    """
    path = _validate_path(request.path)
    project = Path(path)

    preflight = await asyncio.to_thread(_scan_with_defender, project)
    _enforce_defender_result(preflight, project)

    task_id = _new_task(f"Fix dependencies: {project.name}")

    async def run_fix() -> None:
        try:
            callback = get_log_callback(task_id)
            proj_path = Path(path)

            # Step 1: Check for venv
            venv = await asyncio.to_thread(find_venv, proj_path)
            if venv is None:
                callback("🔧 No virtual environment found. Creating one…")
                setup = Setup(path)
                venv_created = await asyncio.to_thread(
                    _call_with_optional_callback, setup.setup_python, callback=callback
                )
                if not venv_created:
                    callback("❌ Could not create virtual environment. Python dependencies cannot be installed.")
                    venv = None
                else:
                    venv = await asyncio.to_thread(find_venv, proj_path)

            # Step 2: Install missing Python packages
            findings = await asyncio.to_thread(check_project_dependencies, proj_path)
            python_missing = []
            node_missing = []
            for f in findings:
                d = f.to_dict()
                if d.get("id") == "dependencies.not_installed":
                    python_missing = d.get("data", {}).get("packages", [])
                elif d.get("id") == "node.not_installed":
                    node_missing = d.get("data", {}).get("packages", [])

            if python_missing:
                if venv is None:
                    callback("⏭️ Skipping Python packages: no virtual environment available.")
                else:
                    callback(f"📦 Installing {len(python_missing)} missing Python package(s): {', '.join(python_missing)}")
                    setup = Setup(path)
                    ok = await asyncio.to_thread(
                        _call_with_optional_callback, setup.install_missing_packages, callback=callback
                    )
                    if not ok:
                        callback("⚠️ Some Python packages may have failed to install. Check the output above.")

            # Step 3: Install missing Node packages
            if node_missing:
                callback(f"📦 Installing {len(node_missing)} missing Node package(s): {', '.join(node_missing)}")
                ok = await asyncio.to_thread(
                    CommandExecutor.run,
                    ["npm", "install"],
                    cwd=str(proj_path),
                    callback=callback,
                )
                if not ok:
                    callback("⚠️ npm install reported errors. Check the output above.")

            # Step 4: Final diagnosis
            callback("\n🔍 Re-checking dependencies after fix…")
            final_findings = await asyncio.to_thread(check_project_dependencies, proj_path)
            final_problems = [f for f in final_findings if f.to_dict().get("status") in ("fail", "warn")]

            if not final_problems:
                callback("✅ All dependencies are now satisfied.")
            else:
                remaining = [f.to_dict()["title"] for f in final_findings if f.to_dict().get("status") in ("fail", "warn")]
                callback(f"⚠️ {len(remaining)} issue(s) still remain: {', '.join(remaining)}")

            _finish_task(task_id, "completed")
        except Exception as e:
            logger.exception("dependency fix failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_fix)
    return {"task_id": task_id, "malware_scan": preflight}


# --------------------------------------------------------------------------- #
#  Health / task listing
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> Dict[str, Any]:
    count = get_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    # "app" says who is answering. Another program may hold the port we
    # wanted, and the launcher needs to tell the two apart.
    return {
        "status": "ok",
        "app": HEALTH_MARKER,
        "version": APP_VERSION,
        "tasks": count,
    }


@app.get("/api/config")
def api_config() -> Dict[str, Any]:
    return {
        "version": APP_VERSION,
        "safe_root": str(_safe_root()),
        "roots": [str(root) for root in allowed_project_roots()],
        "last_project": _last_project(),
        "allow_shell": ALLOW_SHELL,
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
    }


def _last_project() -> str:
    """The project opened last time, if it is still there and still allowed."""
    last = settings_manager.get("paths", "last_project") or ""
    if not last:
        return ""
    try:
        return _validate_path(last)
    except HTTPException:
        return ""


class SafeRootRequest(BaseModel):
    path: str = Field("", max_length=1024)


def _check_new_safe_root(raw: str) -> Path:
    """A projects root must be a real folder, and not a whole drive or Windows itself."""
    if "\x00" in raw:
        raise HTTPException(400, "Invalid path")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(400, "Use a full path, for example C:\\Users\\You\\Projects")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise HTTPException(400, f"Folder does not exist: {resolved}")
    if resolved.parent == resolved:
        raise HTTPException(400, "A whole drive cannot be the projects root; pick a folder on it")
    blocked = [Path.home()]
    if sys.platform == "win32":
        blocked += [Path(os.environ.get("SystemRoot", r"C:\Windows"))]
        blocked += [Path(os.environ[v]) for v in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v)]
    else:
        blocked += [Path(p) for p in ("/etc", "/usr", "/bin", "/sbin", "/var", "/boot", "/root")]
    for item in blocked:
        item = item.resolve()
        if resolved == item or (item != Path.home().resolve() and resolved.is_relative_to(item)):
            raise HTTPException(400, f"{resolved} cannot be the projects root")
    return resolved


@app.post("/settings/safe-root")
def set_safe_root(request: SafeRootRequest) -> Dict[str, Any]:
    """Change the projects root from the dashboard. An empty path goes back to the default.

    Token-protected like every other API call, and only reachable from this PC.
    """
    raw = request.path.strip()
    if raw:
        settings_manager.set("paths", "safe_root", str(_check_new_safe_root(raw)))
    else:
        settings_manager.set("paths", "safe_root", "")
    return {"safe_root": str(_safe_root())}


@app.post("/pick-folder")
async def pick_folder() -> Dict[str, str]:
    """Open the Windows folder picker on this PC and return the chosen folder."""
    def ask() -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return filedialog.askdirectory(initialdir=str(_safe_root()), title="Choose a project folder") or ""
        finally:
            root.destroy()

    try:
        chosen = await asyncio.to_thread(ask)
    except Exception as exc:  # noqa: BLE001 - no display or no tkinter
        raise HTTPException(501, f"The folder picker is not available here: {exc}") from exc
    return {"path": str(Path(chosen).resolve()) if chosen else ""}


@app.post("/open-folder")
def open_folder(request: PathRequest) -> Dict[str, str]:
    """Show the project in Windows Explorer."""
    path = _validate_path(request.path)
    if sys.platform != "win32":
        raise HTTPException(400, "Open in Explorer only works on Windows")
    os.startfile(path)  # type: ignore[attr-defined]  # opens the folder, runs nothing
    return {"opened": path}


# --------------------------------------------------------------------------- #
#  Settings — user preferences for build, execution, and deployment
# --------------------------------------------------------------------------- #

class SettingsUpdateRequest(BaseModel):
    """Request model for updating settings."""
    category: str
    settings: Dict[str, Any]


@app.get("/settings")
def get_settings() -> Dict[str, Any]:
    """Get all current user settings."""
    return settings_manager.all_settings()


@app.post("/settings")
def update_settings(request: SettingsUpdateRequest) -> Dict[str, Any]:
    """Update settings for a specific category.

    Args:
        request: The category and settings to update.

    Returns:
        The updated settings for that category.
    """
    valid_categories = {"build", "execution", "deployment", "ui"}
    if request.category not in valid_categories:
        raise HTTPException(400, f"Invalid category. Must be one of: {', '.join(sorted(valid_categories))}")

    settings_manager.update_category(request.category, request.settings)
    return {
        "category": request.category,
        "settings": settings_manager.get(request.category),
    }


@app.post("/settings/reset")
def reset_settings(category: Optional[str] = Query(None)) -> Dict[str, str]:
    """Reset settings to defaults.

    Args:
        category: If provided, reset only this category. Otherwise reset all.

    Returns:
        Confirmation message.
    """
    settings_manager.reset(category)
    if category:
        return {"message": f"Reset {category} settings to defaults"}
    return {"message": "Reset all settings to defaults"}


@app.get("/projects")
def list_projects(q: str = Query("", max_length=100)) -> List[Dict[str, Any]]:
    """Immediate sub-folders of the safe root with their detected project types."""
    root = _safe_root()
    if not root.is_dir():
        raise HTTPException(404, f"Safe root does not exist: {root}")
    needle = q.strip().lower()
    items: List[Dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith((".", "$")) or child.is_symlink():
            continue
        if needle and needle not in child.name.lower():
            continue
        try:
            types = ProjectDetector(str(child)).detect()
        except Exception:
            types = []
        items.append({"name": child.name, "path": str(child), "types": types})
        if len(items) >= 200:
            break
    return items


@app.get("/where")
def where_is_it(q: str = Query("", max_length=80)) -> Dict[str, Any]:
    """Folders matching a name, with a plain sentence about each one.

    A first visit often knows the program and not the path. The list is
    limited to the projects folders this app is allowed to open.
    """
    roots = allowed_project_roots()
    shown = " and ".join(str(root) for root in roots)
    return {
        "roots": [str(root) for root in roots],
        "looking": f"Looking in {shown}.",
        "matches": find_programs(roots, q),
    }


@app.get("/tasks")
def list_tasks() -> List[Dict[str, Any]]:
    db = get_db()
    rows = db.execute(
        "SELECT task_id, status, label, created_at, updated_at FROM tasks "
        "ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    return [dict(row) for row in rows]

@app.get("/task/{task_id}")
def get_task_status(task_id: str) -> Dict[str, Any]:
    task = _get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


# --------------------------------------------------------------------------- #
#  Analysis
# --------------------------------------------------------------------------- #
def _folder_id(path: Path) -> str:
    """Changes when a folder is deleted and made again, as a re-clone does.

    The dashboard keys "Don't show again" on it, so a fresh copy of a
    project gets the safety reminder again.
    """
    try:
        info = path.stat()
    except OSError:
        return ""
    born = getattr(info, "st_birthtime", None)
    if born is None and os.name == "nt":
        born = info.st_ctime  # creation time on Windows
    return f"{info.st_ino}-{int(born or 0)}"


@app.post("/analyze")
async def analyze_folder(request: AnalyzeRequest) -> Dict[str, Any]:
    path = _validate_path(request.path)
    try:
        result = await asyncio.to_thread(
            lambda: ProjectOrchestrator(path).analyze()
        )
        settings_manager.set("paths", "last_project", path)
        result["folder_id"] = _folder_id(Path(path))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("analyze failed")
        raise HTTPException(400, str(e)) from e


@app.post("/security-scan")
async def security_scan(request: AnalyzeRequest) -> Dict[str, Any]:
    """Run bounded static security checks without executing project code."""
    path = Path(_validate_path(request.path))
    try:
        result = await asyncio.to_thread(SecurityScanner.scan, path)
        malware_scan = await asyncio.to_thread(_scan_with_defender, path)
        result["malware_scan"] = malware_scan
        result = SecurityScanner.apply_malware_scan_context(result, malware_scan)
        return result
    except Exception as e:
        logger.exception("security scan failed")
        raise HTTPException(400, str(e)) from e


@app.post("/security-scan/fix")
async def security_scan_fix(request: AnalyzeRequest) -> Dict[str, Any]:
    """Apply safe rewrites for the current scan, then scan again.

    Does not execute project code. The only edit is shell=True on a
    subprocess call whose command is already a list of strings.
    """
    path = Path(_validate_path(request.path))
    try:
        result = await asyncio.to_thread(SecurityScanner.apply_fixes, path)
        result["malware_scan"] = {
            "status": "skipped",
            "message": "Fixes were applied without running Defender again. Re-scan to include Defender.",
        }
        return result
    except Exception as e:
        logger.exception("security scan fix failed")
        raise HTTPException(400, str(e)) from e


@app.get("/file-watch")
async def file_watch_report() -> Dict[str, Any]:
    """Whether the watch is running, and its alerts.

    While it runs, this read is also the page's heartbeat: when the page
    stops asking for about a minute, the watch stops itself.
    The check does not take a name, an email, or a phone number.
    """
    return file_watch_runner.status()


@app.post("/file-watch/start")
async def file_watch_start() -> Dict[str, Any]:
    """Start the watch. Starting it again while it runs changes nothing."""
    try:
        return file_watch_runner.start()
    except WatchDisabled as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/file-watch/stop")
async def file_watch_stop() -> Dict[str, Any]:
    """Stop the watch now."""
    return file_watch_runner.stop()


@app.post("/privacy-check")
async def privacy_check() -> Dict[str, Any]:
    """Look on this PC for known tracking and remote-control programs.

    The check does not take a name, an email, or a phone number.
    """
    try:
        return await asyncio.to_thread(check_this_pc)
    except Exception as exc:
        logger.exception("privacy check failed")
        raise HTTPException(500, "The privacy check could not finish. See the log for details.") from exc


@app.post("/file-safety-scan")
async def file_safety_scan(request: FileScanRequest) -> Dict[str, Any]:
    path = _validate_file_path(request.path)
    return await asyncio.to_thread(_scan_with_defender, path)


@app.post("/diagnose")
async def diagnose_project(request: AnalyzeRequest) -> Dict[str, Any]:
    path = _validate_path(request.path)
    try:
        return await asyncio.to_thread(ProjectOrchestrator(path).diagnose)
    except Exception as e:
        logger.exception("project diagnosis failed")
        raise HTTPException(400, str(e)) from e


# --------------------------------------------------------------------------- #
#  Workflow — recommended first steps for a project
# --------------------------------------------------------------------------- #

# Priority order: which project types to recommend first
WORKFLOW_PRIORITY = [
    "go", "rust", "java", "kotlin", "scala", "swift",
    "python", "nodejs", "typescript", "dotnet", "csharp",
    "ruby", "php",
    "make", "docker", "git",
]

# Workflow step definitions: maps project types to their recommended actions
WORKFLOW_STEPS = {
    "go": [
        {"label": "📦 Install Go deps", "action": "install-go", "endpoint": "/task/install-dependencies/go", "priority": True},
        {"label": "🔨 Build", "action": "build", "endpoint": "/task/make-target", "param": "build"},
        {"label": "✅ Test", "action": "test", "endpoint": "/task/make-target", "param": "test"},
    ],
    "rust": [
        {"label": "📦 Install Rust deps", "action": "install-rust", "endpoint": "/task/install-dependencies/rust", "priority": True},
        {"label": "🔨 Build", "action": "build", "endpoint": "/task/make-target", "param": "build"},
        {"label": "✅ Test", "action": "test", "endpoint": "/task/make-target", "param": "test"},
    ],
    "python": [
        {"label": "📦 Install Python deps", "action": "install-python", "endpoint": "/task/install-dependencies/python", "priority": True},
        {"label": "🔨 Set up virtual env", "action": "setup", "endpoint": "/task/setup/python"},
        {"label": "✅ Run tests", "action": "test", "endpoint": "/task/make-target", "param": "test"},
    ],
    "nodejs": [
        {"label": "📦 Install Node.js deps", "action": "install-nodejs", "endpoint": "/task/install-dependencies/nodejs", "priority": True},
        {"label": "🔨 Set up project", "action": "setup", "endpoint": "/task/setup/nodejs"},
        {"label": "✅ Run tests", "action": "test", "endpoint": "/task/make-target", "param": "test"},
    ],
    "typescript": [
        {"label": "📦 Install TypeScript deps", "action": "install-typescript", "endpoint": "/task/install-dependencies/nodejs", "priority": True},
        {"label": "🔨 Build", "action": "build", "endpoint": "/task/make-target", "param": "build"},
    ],
    "dotnet": [
        {"label": "📦 Restore .NET deps", "action": "install-dotnet", "endpoint": "/task/install-dependencies/dotnet", "priority": True},
        {"label": "🔨 Build", "action": "build", "endpoint": "/task/make-target", "param": "build"},
        {"label": "✅ Test", "action": "test", "endpoint": "/task/make-target", "param": "test"},
    ],
    "csharp": [
        {"label": "📦 Restore C# deps", "action": "install-csharp", "endpoint": "/task/install-dependencies/dotnet", "priority": True},
        {"label": "🔨 Build", "action": "build", "endpoint": "/task/make-target", "param": "build"},
    ],
    "java": [
        {"label": "📦 Install Java deps", "action": "install-java", "endpoint": "/task/install-dependencies/java", "priority": True},
        {"label": "🔨 Build", "action": "build", "endpoint": "/task/make-target", "param": "build"},
        {"label": "✅ Test", "action": "test", "endpoint": "/task/make-target", "param": "test"},
    ],
    "kotlin": [
        {"label": "📦 Install Kotlin deps", "action": "install-kotlin", "endpoint": "/task/install-dependencies/kotlin", "priority": True},
        {"label": "🔨 Build", "action": "build", "endpoint": "/task/make-target", "param": "build"},
    ],
    "make": [
        {"label": "🔨 Make all", "action": "make-all", "endpoint": "/task/make-target", "param": "make:all"},
        {"label": "🧹 Clean", "action": "make-clean", "endpoint": "/task/make-target", "param": "make:clean"},
    ],
    "docker": [
        {"label": "🐳 Build Docker image", "action": "docker-build", "endpoint": "/task/make-target", "param": "docker"},
    ],
    "ruby": [
        {"label": "📦 Install Ruby deps", "action": "install-ruby", "endpoint": "/task/install-dependencies/ruby", "priority": True},
    ],
    "php": [
        {"label": "📦 Install PHP deps", "action": "install-php", "endpoint": "/task/install-dependencies/php", "priority": True},
    ],
    "swift": [
        {"label": "📦 Install Swift deps", "action": "install-swift", "endpoint": "/task/install-dependencies/swift", "priority": True},
        {"label": "🔨 Build", "action": "build", "endpoint": "/task/make-target", "param": "build"},
    ],
    "scala": [
        {"label": "📦 Install Scala deps", "action": "install-scala", "endpoint": "/task/install-dependencies/scala", "priority": True},
        {"label": "🔨 Build", "action": "build", "endpoint": "/task/make-target", "param": "build"},
    ],
    "git": [],
}


@app.post("/workflow")
async def get_workflow(request: AnalyzeRequest) -> Dict[str, Any]:
    """Generate a recommended workflow for the selected project.
    
    Returns ordered steps grouped by priority so the UI can show
    'what to run first, second, etc.' instead of a flat button wall.
    """
    path = _validate_path(request.path)
    try:
        result = await asyncio.to_thread(
            lambda: ProjectOrchestrator(path).analyze()
        )
        detected = _types_needed_to_run(Path(path), result.get("detected", []))
        
        # Order detected types by priority
        ordered_types = sorted(
            detected,
            key=lambda t: (WORKFLOW_PRIORITY.index(t) if t in WORKFLOW_PRIORITY else 999)
        )
        
        # Always put security scan FIRST — check for malware before running anything
        steps = [
            {"step": 1, "label": "🛡️ Security scan — check for malware & threats", "action": "security-scan", "category": "diagnostics", "project_type": "safety"},
        ]
        step_num = 1

        for ptype in ordered_types:
            ptype_steps = WORKFLOW_STEPS.get(ptype, [])
            for step in ptype_steps:
                step_num += 1
                step_copy = dict(step)
                step_copy["step"] = step_num
                step_copy["project_type"] = ptype
                steps.append(step_copy)

        # Add remaining universal steps at the end
        universal = [
            {"step": step_num + 1, "label": "🔍 Check dependencies", "action": "check-deps", "category": "diagnostics"},
            {"step": step_num + 2, "label": "✅ Quality checks", "action": "quality-checks", "category": "diagnostics"},
        ]
        steps.extend(universal)
        
        return {
            "path": path,
            "detected": detected,
            "ordered_types": ordered_types,
            "steps": steps,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("workflow failed")
        raise HTTPException(400, str(e)) from e


# --------------------------------------------------------------------------- #
#  Setup Guide — progressive step-by-step guide for a project
# --------------------------------------------------------------------------- #

# Maps project types to their essential setup steps (the "happy path")
SETUP_GUIDE_STEPS = {
    "go": [
        {"id": "go-tidy", "label": "Tidy Go modules", "desc": "Download and organize dependencies listed in go.mod", "cmd": "go mod tidy", "endpoint": "/task/make-target", "param": "go.mod"},
        {"id": "go-build", "label": "Build the project", "desc": "Compile the Go program into an exe when this folder is package main", "cmd": "go build -o <name>.exe .", "endpoint": "/task/make-target", "param": "build"},
        {"id": "go-test", "label": "Run tests", "desc": "Run the project's test suite", "cmd": "go test ./...", "endpoint": "/task/make-target", "param": "test"},
    ],
    "rust": [
        {"id": "rust-build", "label": "Build the project", "desc": "Compile the Rust code and download crates", "cmd": "cargo build", "endpoint": "/task/make-target", "param": "build"},
        {"id": "rust-test", "label": "Run tests", "desc": "Run the project's test suite", "cmd": "cargo test", "endpoint": "/task/make-target", "param": "test"},
    ],
    "python": [
        {"id": "python-venv", "label": "Create virtual environment", "desc": "Set up an isolated Python environment for the project", "cmd": "python -m venv venv", "endpoint": "/task/setup/python"},
        {"id": "python-install", "label": "Install dependencies", "desc": "Install packages from requirements.txt or pyproject.toml", "cmd": "pip install -r requirements.txt", "endpoint": "/task/install-dependencies/python"},
        {"id": "python-test", "label": "Run tests", "desc": "Run the project's test suite", "cmd": "pytest", "endpoint": "/task/make-target", "param": "test"},
    ],
    "nodejs": [
        {"id": "node-install", "label": "Install dependencies", "desc": "Install packages from package.json", "cmd": "npm install", "endpoint": "/task/install-dependencies/nodejs"},
        {"id": "node-build", "label": "Build the project", "desc": "Compile or bundle the project", "cmd": "npm run build", "endpoint": "/task/make-target", "param": "build"},
        {"id": "node-test", "label": "Run tests", "desc": "Run the project's test suite", "cmd": "npm test", "endpoint": "/task/make-target", "param": "test"},
    ],
    "typescript": [
        {"id": "ts-install", "label": "Install dependencies", "desc": "Install packages from package.json", "cmd": "npm install", "endpoint": "/task/install-dependencies/nodejs"},
        {"id": "ts-build", "label": "Build the project", "desc": "Compile TypeScript to JavaScript", "cmd": "npm run build", "endpoint": "/task/make-target", "param": "build"},
    ],
    "dotnet": [
        {"id": "dotnet-restore", "label": "Restore dependencies", "desc": "Download NuGet packages", "cmd": "dotnet restore", "endpoint": "/task/install-dependencies/dotnet"},
        {"id": "dotnet-build", "label": "Build the project", "desc": "Compile the .NET code", "cmd": "dotnet build", "endpoint": "/task/make-target", "param": "build"},
        {"id": "dotnet-test", "label": "Run tests", "desc": "Run the project's test suite", "cmd": "dotnet test", "endpoint": "/task/make-target", "param": "test"},
    ],
    "java": [
        {"id": "java-build", "label": "Build the project", "desc": "Compile and package with Maven or Gradle", "cmd": "mvn package", "endpoint": "/task/make-target", "param": "build"},
        {"id": "java-test", "label": "Run tests", "desc": "Run the project's test suite", "cmd": "mvn test", "endpoint": "/task/make-target", "param": "test"},
    ],
    "make": [
        {"id": "make-all", "label": "Build all targets", "desc": "Run the default Makefile target", "cmd": "make", "endpoint": "/task/make-target", "param": "make:all"},
    ],
    "docker": [
        {"id": "docker-build", "label": "Build Docker image", "desc": "Build the container image from Dockerfile", "cmd": "docker build .", "endpoint": "/task/make-target", "param": "docker"},
    ],
    "php": [
        {"id": "php-install", "label": "Install dependencies", "desc": "Install Composer packages", "cmd": "composer install", "endpoint": "/task/install-dependencies/php"},
    ],
    "swift": [
        {"id": "swift-build", "label": "Build the project", "desc": "Compile the Swift code", "cmd": "swift build", "endpoint": "/task/make-target", "param": "build"},
    ],
    "scala": [
        {"id": "scala-build", "label": "Build the project", "desc": "Compile with SBT", "cmd": "sbt compile", "endpoint": "/task/make-target", "param": "build"},
    ],
    "kotlin": [
        {"id": "kotlin-build", "label": "Build the project", "desc": "Compile with Gradle", "cmd": "./gradlew build", "endpoint": "/task/make-target", "param": "build"},
    ],
    "ruby": [
        {"id": "ruby-install", "label": "Install dependencies", "desc": "Install gems from Gemfile", "cmd": "bundle install", "endpoint": "/task/install-dependencies/ruby"},
    ],
}


def _split_guide_steps(detected: List[str]) -> tuple:
    """Essential steps, primary types, supporting types, and the last step number.

    The security scan is always first. Language steps follow in detection
    order. Git, Docker and Make stay in the supporting list.
    """
    primary_types = [item for item in detected if item not in ("git", "docker", "make")]
    supporting_types = [item for item in detected if item in ("git", "docker", "make")]
    essential_steps: List[Dict[str, Any]] = [{
        "step": 1,
        "id": "security-scan",
        "label": "🛡️ Security scan",
        "desc": "Check the project for malware and threats before running anything",
        "action": "security-scan",
        "category": "essential",
        "cmd": "Security scan",
        "recommended": True,
    }]
    step_num = 1
    for project_type in primary_types:
        for step in SETUP_GUIDE_STEPS.get(project_type, []):
            step_num += 1
            step_copy = dict(step)
            step_copy["step"] = step_num
            step_copy["category"] = "essential"
            step_copy["project_type"] = project_type
            step_copy["recommended"] = True
            essential_steps.append(step_copy)
    return essential_steps, primary_types, supporting_types, step_num


@app.post("/setup-guide")
async def get_setup_guide(request: AnalyzeRequest) -> Dict[str, Any]:
    """Generate a progressive step-by-step setup guide for the selected project.

    Unlike the workflow (which lists everything), this returns a curated
    checklist of essential steps in the right order, with optional advanced
    steps hidden behind a toggle.  Each step includes a description so the
    user knows what it does before running it.
    """
    path = _validate_path(request.path)
    try:
        result = await asyncio.to_thread(
            lambda: ProjectOrchestrator(path).analyze()
        )
        detected = _types_needed_to_run(Path(path), result.get("detected", []))
        essential_steps, primary_types, supporting_types, step_num = _split_guide_steps(detected)
        _annotate_go_build(Path(path), essential_steps)

        # Build optional/advanced steps from supporting tools
        optional_steps = []
        for stype in supporting_types:
            stype_steps = SETUP_GUIDE_STEPS.get(stype, [])
            for step in stype_steps:
                step_num += 1
                step_copy = dict(step)
                step_copy["step"] = step_num
                step_copy["category"] = "optional"
                step_copy["project_type"] = stype
                optional_steps.append(step_copy)

        # Add universal optional steps
        universal_optional = [
            {"step": step_num + 1, "id": "check-deps", "label": "🔍 Check dependencies", "desc": "Look for missing dependencies and files", "action": "check-deps", "category": "optional"},
            {"step": step_num + 2, "id": "quality-checks", "label": "✅ Quality checks", "desc": "Run linters, formatters, and other quality tools", "action": "quality-checks", "category": "optional"},
        ]
        optional_steps.extend(universal_optional)

        # Read README for project-specific instructions
        readme_tip = None
        project = Path(path)
        for readme_name in ("README.md", "README", "readme.md"):
            readme_path = project / readme_name
            if readme_path.is_file():
                try:
                    content = readme_path.read_text(encoding="utf-8", errors="replace")
                    # Extract installation section if present
                    import re
                    install_match = re.search(r'(?i)##\s*install', content)
                    start = install_match.start() if install_match else 0
                    snippet = content[start:start + 500].strip()
                    if snippet:
                        readme_tip = snippet.replace("\n", " ")[:300]
                except OSError:
                    pass
                break

        for step in essential_steps:
            step["recommended"] = True
        for step in optional_steps:
            step["recommended"] = False
        # Parts of the project in sub-folders (PhoneInfoga's web/client) are
        # set up on their own: mention them instead of piling on buttons.
        sub_projects = [
            {"path": item["path"], "detected": item.get("detected", [])}
            for item in result.get("projects", [])
            if Path(item["path"]) != project and item.get("detected")
        ]

        return {
            "path": path,
            "detected": detected,
            "sub_projects": sub_projects,
            "primary_types": primary_types,
            "supporting_types": supporting_types,
            "essential_steps": essential_steps,
            "optional_steps": optional_steps,
            "readme_tip": readme_tip,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("setup guide failed")
        raise HTTPException(400, str(e)) from e


class GuideRequest(BaseModel):
    path: Optional[str] = None
    activity: Literal["browse", "project", "running", "finished", "failed"] = "browse"
    task_label: str = Field("", max_length=200)
    task_status: str = Field("", max_length=40)
    use_ai: bool = False


def _guide_for_path(path: str) -> tuple:
    detected = ProjectOrchestrator(path).analyze().get("detected") or []
    detected = _types_needed_to_run(Path(path), list(detected))
    steps, _, _, _ = _split_guide_steps(detected)
    _annotate_go_build(Path(path), steps)
    return list(detected), steps


# A detected type is only set up when the program needs that tool to run.
# Types with no entry here are left as the detector found them.
_RUNTIME_TOOL = {
    "python": "python",
    "nodejs": "node",
    "typescript": "node",
    "go": "go",
    "docker": "docker",
    "make": "make",
}


def _types_needed_to_run(path: Path, detected: List[str]) -> List[str]:
    from doctor.diagnostics import required_tools

    needed = set(required_tools(path))
    kept: List[str] = []
    for project_type in detected:
        tool = _RUNTIME_TOOL.get(project_type)
        if tool is None or tool in needed:
            kept.append(project_type)
    return kept


def _guide_with_model(
    advice: Dict[str, Any],
    detected: List[str],
    task_label: str,
    task_status: str,
) -> Dict[str, Any]:
    """Ask Ollama to pick one of the real steps. Any failure keeps the built-in advice."""
    try:
        client = OllamaClient(timeout=20)
        if client.status().get("state") != "ready":
            return advice
        lines = [
            f"- {step['id']}: {step['label']} — {step.get('desc', '')}"
            for step in advice.get("choices") or []
        ]
        prompt = (
            "Choose the single best next action from the list. "
            'Reply with JSON only: {"action_id":"<id from the list, or none>","why":"<one short sentence>"}\n'
            "Do not invent actions or commands.\n\n"
            f"Activity: {advice.get('activity')}\n"
            f"Last task: {task_label or '(none)'} ({task_status or 'n/a'})\n"
            f"Detected: {', '.join(detected) or 'none'}\n"
            f"Built-in recommendation: {advice.get('headline')} — {advice.get('detail')}\n"
            "Actions:\n" + ("\n".join(lines) if lines else "(none)")
        )
        result = client.chat(
            [
                {"role": "system", "content": "Reply with JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=180,
        )
    except (OllamaError, OSError, ValueError):
        return advice
    return apply_model_reply(advice, str(result.get("reply") or ""))


@app.post("/guide/advice")
async def guide_advice(request: GuideRequest) -> Dict[str, Any]:
    """Recommend the next action for what the user is doing.

    The choice comes from the project's real steps. When ``use_ai`` is set
    and Ollama is ready, the model may pick one of those steps and rewrite
    the sentence. It cannot add a command of its own. Ollama being down
    still returns the built-in recommendation.
    """
    activity = request.activity
    detected: List[str] = []
    steps: List[Dict[str, Any]] = []
    path = (request.path or "").strip()
    if activity != "browse" and path:
        path = _validate_path(path)
        if activity != "running":
            try:
                detected, steps = await asyncio.to_thread(_guide_for_path, path)
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("guide advice failed")
                raise HTTPException(400, str(exc)) from exc
    elif activity not in {"browse", "running"}:
        activity = "browse"
    advice = advise(
        activity=activity,
        task_label=request.task_label,
        detected=detected,
        steps=steps,
    )
    if request.use_ai and activity not in {"browse", "running"}:
        advice = await asyncio.to_thread(
            _guide_with_model, advice, detected, request.task_label, request.task_status
        )
    return present(advice)


def _dependency_install_commands(project_type: str, project: Path) -> List[List[str]]:
    """The exact commands "Install dependencies" runs, in order.

    Used both to run the install and to show the user a preview first.
    """
    path = str(project)
    pre_commands: List[List[str]] = []
    if project_type == "python":
        # A project can declare its dependencies in requirements.txt or in
        # pyproject.toml; both are installed the same way.
        requirements = Setup(path).python_install_requirements()
        if not requirements:
            raise HTTPException(400, "No supported dependency manifest found for this project type")
        venv_python = _project_python(project)
        if venv_python is None:  # never install into Setup Manager's own environment
            pre_commands.append([sys.executable, "-m", "venv", str(project / "venv")])
            venv_python = project / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        command = [str(venv_python), "-m", "pip", "install", "--only-binary=:all:", *requirements]
    elif project_type == "nodejs" and (project / "package.json").is_file():
        from doctor.diagnostics import required_tools
        if "node" not in required_tools(project):
            raise HTTPException(400, "This project does not need Node packages to run")
        manager = (
            "pnpm" if (project / "pnpm-lock.yaml").exists()
            else "yarn" if (project / "yarn.lock").exists()
            else "npm"
        )
        if shutil.which(manager) is None:
            raise HTTPException(400, f"{manager} is required but was not found on PATH")
        command = [manager, "install", "--ignore-scripts"]
    elif project_type == "go" and (project / "go.mod").is_file():
        from doctor.diagnostics import required_tools
        if "go" not in required_tools(project):
            raise HTTPException(400, "This project does not need Go modules to run")
        if shutil.which("go") is None:
            raise HTTPException(400, "Go is required but was not found on PATH")
        command = ["go", "mod", "download"]
    elif project_type == "dotnet" and (
        any(project.glob("*.csproj")) or any(project.glob("*.sln"))
    ):
        if shutil.which("dotnet") is None:
            raise HTTPException(400, ".NET SDK is required but was not found on PATH")
        command = ["dotnet", "restore"]
    else:
        raise HTTPException(400, "No supported dependency manifest found for this project type")

    return [*pre_commands, command]


@app.post("/task/install-dependencies/{project_type}")
async def install_dependencies(
    project_type: str,
    background_tasks: BackgroundTasks,
    path: str = Query(..., description="Selected project directory"),
) -> Dict[str, Any]:
    path = _validate_path(path)
    project = Path(path)
    commands = _dependency_install_commands(project_type, project)
    preflight = await asyncio.to_thread(_scan_with_defender, project)
    _enforce_defender_result(preflight, project)

    task_id = _new_task(f"Install {project_type} dependencies: {project.name}")

    async def run_install() -> None:
        try:
            success = True
            for step in commands:
                ok = await asyncio.to_thread(
                    CommandExecutor.run, step, cwd=path, callback=get_log_callback(task_id)
                )
                if not ok:
                    retry = source_retry_command(step, project)
                    if retry is not None:
                        get_log_callback(task_id)(
                            "A package has no wheel. Retrying so pip can build that package from source."
                        )
                        ok = await asyncio.to_thread(
                            CommandExecutor.run, retry, cwd=path, callback=get_log_callback(task_id)
                        )
                success = success and ok
            if success:
                postflight = await asyncio.to_thread(_scan_with_defender, project)
                _enforce_defender_result(postflight, project)
                if postflight.get("status") == "unavailable":
                    get_log_callback(task_id)(f"Warning: {postflight.get('message', 'Malware scanner unavailable.')}")
            _finish_task(task_id, "completed" if success else "failed")
        except Exception as e:
            logger.exception("dependency installation failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_install)
    return {"task_id": task_id, "malware_scan": preflight}


class PlanRequest(BaseModel):
    action: Literal["install-dependencies", "install-missing", "fix-dependencies", "setup", "extensions", "repair"]
    path: str = Field(..., min_length=1)
    project_type: Optional[str] = None
    repair_id: Optional[str] = None


def _show(command: List[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])


def _missing_install_command(project: Path) -> Optional[List[str]]:
    setup = Setup(str(project))
    missing = setup.missing_requirements()
    venv_python = setup._venv_python()
    if not missing or venv_python is None:
        return None
    return [str(venv_python), "-m", "pip", "install", "--only-binary=:all:",
            *[requirement.to_pip() for requirement in missing]]


def _find_repair(project: Path, repair_id: str):
    found = run_machine_diagnostics(project)
    found.extend(check_project_dependencies(project))
    for repair in available_repairs(found, project):
        if repair.id == repair_id:
            return repair
    raise HTTPException(404, "That repair is not needed for this project any more")


@app.post("/plan")
async def plan(request: PlanRequest) -> Dict[str, Any]:
    """Dry run: what an install or repair *would* do. Nothing is run here.

    The dashboard shows this and waits for the user to click Approve.
    """
    path = _validate_path(request.path)
    project = Path(path)
    commands: List[str] = []
    notes: List[str] = []
    if request.action == "install-dependencies":
        steps = await asyncio.to_thread(
            _dependency_install_commands, request.project_type or "", project
        )
        commands = [_show(step) for step in steps]
        if (request.project_type or "") == "python":
            left_out = [
                item.name for item in declared_requirements(project).skipped
                if "not needed to run" in item.reason or "not part of what the program needs" in item.reason
            ]
            if left_out:
                preview = ", ".join(left_out[:12])
                notes.append(f"Left out, not needed to run: {preview}")
    elif request.action == "install-missing":
        step = await asyncio.to_thread(_missing_install_command, project)
        if step:
            commands = [_show(step)]
        else:
            notes.append("Nothing to install, or the project has no virtual environment yet (run 'Set up python' first).")
    elif request.action == "fix-dependencies":
        if await asyncio.to_thread(find_venv, project) is None:
            notes.append("Creates a virtual environment first (the 'Set up python' steps).")
        step = await asyncio.to_thread(_missing_install_command, project)
        if step:
            commands.append(_show(step))
        if (project / "package.json").is_file():
            commands.append("npm install   (only if Node packages are missing)")
        notes.append("Missing packages are re-checked when it is done.")
    elif request.action == "setup":
        notes.append(
            f"Runs the built-in '{request.project_type}' setup for this folder. "
            "Every command it runs is printed in the Output panel as it goes."
        )
    elif request.action == "extensions":
        notes.append("Installs linters, type checkers, test tools and security scanners into the project's own environment.")
    elif request.action == "repair":
        repair = await asyncio.to_thread(_find_repair, project, request.repair_id or "")
        notes.append(f"{repair.title}: {repair.description}")
    notes.append("A malware scan of the folder runs first.")
    return {"action": request.action, "path": path, "commands": commands, "notes": notes}


class RepairRequest(BaseModel):
    path: str = Field(..., min_length=1)
    repair_id: str = Field(..., min_length=1)


@app.post("/doctor/repair")
async def doctor_repair(request: RepairRequest) -> Dict[str, Any]:
    """Apply one repair the doctor offered. Verified fixes are remembered."""
    project = Path(_validate_path(request.path))
    repair = await asyncio.to_thread(_find_repair, project, request.repair_id)
    return await asyncio.to_thread(BuildDoctor(project).apply, repair)


@app.post("/task/install-missing")
async def install_missing(
    request: Dict[str, Any],
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """Install the dependencies a project declares but does not have.

    Body: {"path": "<project directory>"}. Only packages the project
    declares are installed, into the project's own environment; a module
    the code imports but does not declare might be a package or a file
    that belongs in the project, and pip cannot tell the difference.
    """
    path = _validate_path(request.get("path") or "")
    project = Path(path)

    preflight = await asyncio.to_thread(_scan_with_defender, project)
    _enforce_defender_result(preflight, project)

    task_id = _new_task(f"Install missing dependencies: {project.name}")

    async def run_install() -> None:
        try:
            callback = get_log_callback(task_id)
            setup = Setup(path)
            success = await asyncio.to_thread(
                _call_with_optional_callback, setup.install_missing_packages, callback=callback
            )
            _finish_task(task_id, "completed" if success else "failed")
        except Exception as e:
            logger.exception("installing missing dependencies failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_install)
    return {"task_id": task_id, "malware_scan": preflight}

# --------------------------------------------------------------------------- #
#  Project Launch
# --------------------------------------------------------------------------- #
def _launch_program_available(command: List[str], project: Path) -> bool:
    """True when the launch command's program is on PATH or is a project file.

    ``gradlew.bat`` and ``./gradlew`` are not installed programs. ``shutil.which``
    does not look in the project folder, so a wrapper that is sitting right
    there used to be reported as missing.
    """
    if not command:
        return False
    head = command[0]
    if (project / head).is_file():
        return True
    if head.startswith(("./", ".\\")) and (project / head[2:]).is_file():
        return True
    return CommandExecutor.check_installed(head)


@app.post("/task/launch")
async def launch_project(
    request: LaunchRequest,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """Launch a detected project candidate with pre-flight security scan."""
    path = _validate_path(request.path)
    project = Path(path)

    # Pre-flight malware scan
    preflight = await asyncio.to_thread(_scan_with_defender, project)
    _enforce_defender_result(preflight, project)

    # Get launch candidates
    candidates = await asyncio.to_thread(ProjectOrchestrator(path)._get_launch_candidates)
    if request.candidate_index >= len(candidates):
        raise HTTPException(400, "The selected launch option is no longer available")

    candidate = candidates[request.candidate_index]

    # The button names a program. If that program is not on the machine the
    # run only fails later, so it is checked first and said out loud.
    command = list(candidate["command"])
    if command and not _launch_program_available(command, project):
        raise HTTPException(
            400,
            f"{command[0]} is not installed, so '{candidate['label']}' cannot run. "
            "Install it and try again.",
        )

    task_id = _new_task(f"Launch: {candidate['label']}")

    async def run_candidate() -> None:
        try:
            callback = get_log_callback(task_id)
            callback(f"🚀 Launching {candidate['label']} in {path} (Mode: {request.run_mode})")
            callback(f"   Command: {' '.join(candidate['command'])}")

            # Special handling for Docker Compose - run detached
            command = list(candidate["command"]) # Ensure mutable
            if "docker" in command and "compose" in command and "up" in command:
                if "-d" not in command:
                    command.append("-d")
                if "--build" not in command:
                    command.append("--build")
                callback("   Added -d (detached) and --build flags for Docker Compose")

            success = await asyncio.to_thread(
                CommandExecutor.run,
                command,
                cwd=path,
                allow_shell=False,
                timeout=600,  # 10 min timeout for Docker builds
                callback=callback,
                run_mode=request.run_mode,
            )

            if success:
                # Post-flight scan
                postflight = await asyncio.to_thread(_scan_with_defender, project)
                if postflight.get("status") == "threats":
                    callback("⚠️ WARNING: Post-execution scan detected new threats!")
                else:
                    callback("✅ Launch completed successfully")

            _finish_task(task_id, "completed" if success else "failed")
        except Exception as e:
            logger.exception("project launch failed")
            get_log_callback(task_id)(f"❌ Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_candidate)
    return {"task_id": task_id, "malware_scan": preflight}

def _mount_disk_image(image_path: str, dismount: bool = False) -> Dict[str, Any]:
    if sys.platform != "win32":
        raise HTTPException(400, "Disk image mounting is supported only on Windows")
    image = Path(image_path).expanduser().resolve()
    if not image.is_file() or image.suffix.lower() not in {".iso", ".vhd", ".vhdx"}:
        raise HTTPException(400, "Choose an existing .iso, .vhd, or .vhdx file")
    malware_scan = None
    if not dismount:
        malware_scan = _scan_with_defender(image)
        _enforce_defender_result(malware_scan, image)

    escaped_path = str(image).replace("'", "''")
    if dismount:
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"Dismount-DiskImage -ImagePath '{escaped_path}'; "
            "Write-Output 'Disk image dismounted.'"
        )
    else:
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$disk = Mount-DiskImage -ImagePath '{escaped_path}' -PassThru; "
            "$disk | Get-Volume | Select-Object DriveLetter, FileSystemLabel | ConvertTo-Json -Compress"
        )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(500, f"Could not run Windows disk-image command: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "PowerShell mount operation failed"
        raise HTTPException(400, detail)
    return {"image_path": str(image), "message": result.stdout.strip(), "malware_scan": malware_scan}


@app.post("/mount")
async def mount_image(request: MountRequest) -> Dict[str, Any]:
    image = _validate_file_path(request.image_path)
    return await asyncio.to_thread(_mount_disk_image, str(image))


@app.post("/dismount")
async def dismount_image(request: MountRequest) -> Dict[str, Any]:
    image = _validate_file_path(request.image_path)
    return await asyncio.to_thread(_mount_disk_image, str(image), True)


# --------------------------------------------------------------------------- #
#  Environment setup
# --------------------------------------------------------------------------- #
@app.post("/task/setup/{env_type}")
async def setup_env(
    env_type: str,
    background_tasks: BackgroundTasks,
    path: str = Query(..., description="Project directory"),
) -> Dict[str, str]:
    path = _validate_path(path)
    project = Path(path)

    # Pre-flight malware scan: setup runs code from the project (pip
    # installs it, npm runs its lifecycle scripts, git runs its hooks).
    preflight = await asyncio.to_thread(_scan_with_defender, project)
    _enforce_defender_result(preflight, project)

    task_id = _new_task(f"Setup {env_type}: {Path(path).name}")

    async def run_setup() -> None:
        try:
            setup = Setup(path)
            callback = get_log_callback(task_id)

            handlers: Dict[str, Optional[Callable[..., Any]]] = {
                "python": getattr(setup, "setup_python", None),
                "nodejs": getattr(setup, "setup_nodejs", None),
                "dotnet": getattr(setup, "setup_dotnet", None),
                "ruby": getattr(setup, "setup_ruby", None),
                "git": getattr(setup, "setup_git", None),
                "make": getattr(setup, "setup_make", None),
            }

            handler = handlers.get(env_type)
            if handler is None:
                callback(f"Unknown environment: {env_type}")
                _finish_task(task_id, "failed")
                return

            success = await asyncio.to_thread(
                _call_with_optional_callback, handler, callback=callback
            )
            _finish_task(task_id, "completed" if success else "failed")
        except Exception as e:
            logger.exception("setup_env failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_setup)
    return {"task_id": task_id}


# --------------------------------------------------------------------------- #
#  Extensions
# --------------------------------------------------------------------------- #
@app.post("/task/extensions")
async def install_ext(
    request: ExtensionRequest,
    background_tasks: BackgroundTasks,
    path: str = Query(..., description="Project directory"),
) -> Dict[str, str]:
    path = _validate_path(path)
    project = Path(path)

    # Pre-flight malware scan: installing packages runs the project's
    # own build scripts.
    preflight = await asyncio.to_thread(_scan_with_defender, project)
    _enforce_defender_result(preflight, project)

    task_id = _new_task(f"Extensions: {Path(path).name}")

    async def run_install() -> None:
        try:
            setup = Setup(path)
            callback = get_log_callback(task_id)
            project_types = (
                ProjectDetector(path).detect()
                if request.project_type == "auto"
                else [request.project_type]
            )
            supported_types = [
                project_type for project_type in project_types
                if project_type in {"python", "nodejs", "dotnet"}
            ]
            success = True
            for project_type in supported_types:
                success = await asyncio.to_thread(
                    setup.install_extensions,
                    project_type,
                    request.ext_types,
                    callback=callback,
                ) and success
            _finish_task(task_id, "completed" if success else "failed")
        except Exception as e:
            logger.exception("install_ext failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_install)
    return {"task_id": task_id}


# --------------------------------------------------------------------------- #
#  Quality checks
# --------------------------------------------------------------------------- #
def _checks_status(results: Dict[str, Any]) -> str:
    """A task that ran checks reports failure when a check failed.

    Reporting every run as "completed" hid real failures from the user.
    """
    for value in (results or {}).values():
        if str(value).strip().lower() in {"fail", "failed", "error"}:
            return "failed"
    return "completed"


@app.post("/task/checks")
async def run_checks(
    background_tasks: BackgroundTasks,
    project_type: str = Query(...),
    path: str = Query(...),
) -> Dict[str, str]:
    path = _validate_path(path)
    project = Path(path)

    # Pre-flight malware scan: the test runner loads the project's own
    # conftest.py and test files, which is code execution.
    preflight = await asyncio.to_thread(_scan_with_defender, project)
    _enforce_defender_result(preflight, project)

    task_id = _new_task(f"Quality checks: {Path(path).name}")

    async def run_checks_task() -> None:
        try:
            setup = Setup(path)
            callback = get_log_callback(task_id)
            project_types = (
                ProjectDetector(path).detect()
                if project_type == "auto"
                else [project_type]
            )
            results: Dict[str, str] = {}
            for detected_type in project_types:
                if detected_type not in {"python", "nodejs", "dotnet"}:
                    continue
                type_results = await asyncio.to_thread(
                    setup.run_quality_checks, detected_type, callback=callback
                )
                results.update({
                    f"{detected_type}: {name}": status
                    for name, status in type_results.items()
                })
            _finish_task(task_id, _checks_status(results), results=results)
        except Exception as e:
            logger.exception("run_checks failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_checks_task)
    return {"task_id": task_id}


# --------------------------------------------------------------------------- #
#  Custom command
# --------------------------------------------------------------------------- #
@app.post("/task/custom")
async def custom_cmd(
    request: CustomCommandRequest, background_tasks: BackgroundTasks
) -> Dict[str, str]:
    if not request.confirm:
        raise HTTPException(400, "Confirmation required for custom commands")
    blocked = refusal(request.command)
    if blocked:
        logger.warning("AUDIT blocked risky custom command: %s", request.command)
        raise HTTPException(400, blocked)
    cwd = _validate_path(request.cwd or str(_safe_root()))
    logger.info("AUDIT custom command (shell=%s) in %s: %s", ALLOW_SHELL, cwd, request.command)
    task_id = _new_task(f"Custom: {request.command[:60]}")

    async def run_cmd() -> None:
        try:
            callback = get_log_callback(task_id)
            success = await asyncio.to_thread(
                CommandExecutor.run,
                request.command,
                cwd=cwd,
                allow_shell=ALLOW_SHELL,  # default: parsed into an argv list, no shell
                callback=callback,
            )
            _finish_task(task_id, "completed" if success else "failed")
        except Exception as e:
            logger.exception("custom_cmd failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_cmd)
    return {"task_id": task_id}


# --------------------------------------------------------------------------- #
#  Automation Endpoints
# --------------------------------------------------------------------------- #

@app.post("/task/outdated")
async def task_outdated(request: PathRequest, background_tasks: BackgroundTasks) -> Dict[str, str]:
    path = _validate_path(request.path)
    task_id = _new_task(f"Outdated packages: {Path(path).name}")

    async def run_outdated() -> None:
        try:
            callback = get_log_callback(task_id)
            project = Path(path)

            # Determine command based on project type
            if (project / "package.json").exists():
                cmd = ["npm", "outdated"]
            elif (project / "requirements.txt").exists():
                venv_python = _project_python(project)
                if venv_python is None:
                    callback("No project virtual environment found. Run the Python setup first.")
                    _finish_task(task_id, "failed")
                    return
                cmd = [str(venv_python), "-m", "pip", "list", "--outdated"]
            else:
                callback("No supported project manifest found to check for outdated packages.")
                _finish_task(task_id, "failed")
                return

            callback(f"Checking for outdated packages in {path}...")
            success = await asyncio.to_thread(
                CommandExecutor.run, cmd, cwd=path, callback=callback
            )
            _finish_task(task_id, "completed" if success else "failed")
        except Exception as e:
            logger.exception("task_outdated failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_outdated)
    return {"task_id": task_id}


@app.post("/project-check")
async def project_check(request: PathRequest) -> Dict[str, Any]:
    """Missing files and folders, and which declared updates are safe to download.

    The folder is only read. Nothing is downloaded and nothing is installed.
    """
    path = _validate_path(request.path)
    return await asyncio.to_thread(check_project, Path(path))


@app.post("/project-check/download")
async def project_check_download(request: PathRequest) -> Dict[str, Any]:
    """Save declared updates that pass the safety check.

    The addresses come from the check, not from the request. A file is
    saved only when it is a checksummed wheel or npm tarball from the
    official registry. Nothing is installed.
    """
    path = _validate_path(request.path)
    return await asyncio.to_thread(download_safe_updates, Path(path))


@app.post("/task/generate-env")
async def task_generate_env(request: PathRequest, background_tasks: BackgroundTasks) -> Dict[str, str]:
    path = _validate_path(request.path)
    task_id = _new_task(f"Generate .env: {Path(path).name}")

    async def run_generate_env() -> None:
        try:
            callback = get_log_callback(task_id)
            project_root = Path(path)
            env_example = project_root / ".env.example"
            env_file = project_root / ".env"

            if not env_example.exists():
                callback("Error: .env.example not found in project root.")
                _finish_task(task_id, "failed")
                return

            if env_file.exists():
                callback(".env already exists. Skipping generation.")
                _finish_task(task_id, "completed")
                return

            callback("Generating .env from .env.example...")
            shutil.copy2(env_example, env_file)
            callback("✅ .env file created successfully.")
            _finish_task(task_id, "completed")
        except Exception as e:
            logger.exception("task_generate_env failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_generate_env)
    return {"task_id": task_id}


_MAKE_TARGET_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


def _explicit_target_command(target: str, detected: List[str], project: Path) -> Optional[List[str]]:
    """Targets that name their tool, so a Go+Make+Docker project runs the right one.

    "make:<name>" runs that Makefile target; "docker"/"docker-run" use Docker.
    """
    if target.startswith("make:") and "make" in detected and _MAKE_TARGET_NAME.match(target[5:]):
        return [make_command() or "make", target[5:]]
    if "docker" in detected and target == "docker":
        return ["docker", "build", "-t", project.name.lower(), "."]
    if "docker" in detected and target == "docker-run":
        # No -it: the task has no console, and Docker rejects a TTY it cannot open.
        return ["docker", "run", "--rm", project.name.lower()]
    return None


def _go_major_version(module_path: str) -> tuple:
    """Split a Go import path into the module and its major version.

    ``github.com/acme/tool/v2/cmd`` becomes ``("github.com/acme/tool", "2")``.
    A path with no ``/vN`` segment keeps an empty version.
    """
    parts = module_path.split("/")
    for index in range(1, len(parts)):
        if re.fullmatch(r"v\d+", parts[index]):
            return "/".join(parts[:index]), parts[index][1:]
    return module_path, ""


def _read_go_module(project: Path) -> str:
    path = project / "go.mod"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"(?m)^module\s+(\S+)", text)
    return match.group(1) if match else ""


def _go_module_mismatch(project: Path) -> Optional[str]:
    """Explain when go.mod names a different module than the source imports.

    Go then compiles the downloaded module. PhoneInfoga's folder said v3
    while every file imported v2, so the build used a cache copy that had
    no web files.
    """
    module = _read_go_module(project)
    if not module:
        return None
    base, major = _go_major_version(module)
    found: Dict[str, int] = {}
    seen = 0
    root = project.resolve()
    skip = {"vendor", "node_modules", ".git"}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [name for name in dirnames if name not in skip and not name.startswith(".")]
        if here != root and (here / "go.mod").is_file():
            dirnames[:] = []
            continue
        for name in filenames:
            if not name.endswith(".go"):
                continue
            seen += 1
            if seen > 500:
                break
            path = here / name
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for imported in re.findall(r'"([^"\n]+)"', text):
                other_base, other_major = _go_major_version(imported)
                if other_base == base and other_major != major:
                    key = f"{other_base}/v{other_major}" if other_major else other_base
                    found[key] = found.get(key, 0) + 1
    if not found:
        return None
    other = max(found, key=found.get)
    return (
        f"go.mod says this folder is {module}, but the code imports {other}. "
        "Go would compile the downloaded module instead of this folder. "
        f"Change the module line to {other} and remove the require line for that same module."
    )


def _go_folder_is_main(project: Path) -> bool:
    """True when a Go file in the project root declares package main."""
    for path in sorted(project.glob("*.go")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
                continue
            return stripped == "package main"
    return False


def _go_build_command(project: Path) -> List[str]:
    """Build a program to an exe, or type-check a library that has no main."""
    if _go_folder_is_main(project):
        suffix = ".exe" if os.name == "nt" else ""
        return ["go", "build", "-o", project.name.lower() + suffix, "."]
    return ["go", "build", "./..."]


def _annotate_go_build(project: Path, steps: List[Dict[str, Any]]) -> None:
    """Show the command Build will actually run for this folder."""
    command = _go_build_command(project)
    for step in steps:
        if step.get("id") != "go-build":
            continue
        step["cmd"] = " ".join(command)
        if "-o" in command:
            step["desc"] = f"Compile this program to {command[3]}"
        else:
            step["desc"] = "Compile the Go packages and check for errors"


@app.post("/task/make-target")
async def task_make_target(
    target: str = Query(..., description="Make target name (e.g. build, test, clean)"),
    path: str = Query(..., description="Project directory"),
    background_tasks: BackgroundTasks = None,
) -> Dict[str, str]:
    """Run a make target for the project. Detects the project type and runs the appropriate command."""
    path = _validate_path(path)
    project = Path(path)
    task_id = _new_task(f"Make target: {target}")

    async def run_make_target() -> None:
        try:
            callback = get_log_callback(task_id)
            
            # Detect project type and run appropriate command
            detected = ProjectDetector(path).detect()
            cmd = _explicit_target_command(target, detected, project)
            if cmd is not None:
                pass
            elif target.startswith("make:"):
                callback(f"Not a valid make target: {target[5:]}")
                _finish_task(task_id, "failed")
                return
            elif "go" in detected:
                if target in {"build", "test", "go.mod", "go.sum"}:
                    mismatch = _go_module_mismatch(project)
                    if mismatch:
                        callback(mismatch)
                        _finish_task(task_id, "failed")
                        return
                if target == "build":
                    cmd = _go_build_command(project)
                elif target == "test":
                    cmd = ["go", "test", "./..."]
                elif target == "clean":
                    cmd = ["go", "clean"]
                elif target == "fmt":
                    cmd = ["go", "fmt", "./..."]
                elif target == "vet":
                    cmd = ["go", "vet", "./..."]
                elif target == "go.mod":
                    cmd = ["go", "mod", "tidy"]
                elif target == "go.sum":
                    cmd = ["go", "mod", "download"]
                else:
                    callback(f"Unknown Go target: {target}")
                    _finish_task(task_id, "failed")
                    return
            elif "rust" in detected:
                if target == "build":
                    cmd = ["cargo", "build"]
                elif target == "test":
                    cmd = ["cargo", "test"]
                elif target == "clean":
                    cmd = ["cargo", "clean"]
                elif target == "fmt":
                    cmd = ["cargo", "fmt"]
                elif target == "lint":
                    cmd = ["cargo", "clippy"]
                else:
                    callback(f"Unknown Rust target: {target}")
                    _finish_task(task_id, "failed")
                    return
            elif "python" in detected:
                if target == "test":
                    venv_python = _project_python(project)
                    if venv_python:
                        python = str(venv_python)
                        missing = await asyncio.to_thread(
                            _missing_python_test_packages, python, project
                        )
                        if missing:
                            callback(
                                "Installing packages that are not in this project: "
                                + ", ".join(missing)
                            )
                            installed = await asyncio.to_thread(
                                CommandExecutor.run,
                                [python, "-m", "pip", "install", *missing],
                                cwd=path,
                                callback=callback,
                            )
                            if not installed:
                                _finish_task(task_id, "failed")
                                return
                        cmd = [python, "-m", "pytest"]
                    else:
                        cmd = ["python", "-m", "pytest"]
                elif target == "lint":
                    cmd = ["python", "-m", "pylint", "."]
                elif target == "fmt":
                    cmd = ["python", "-m", "black", "."]
                else:
                    callback(f"Unknown Python target: {target}")
                    _finish_task(task_id, "failed")
                    return
            elif "nodejs" in detected or "typescript" in detected:
                manager = (
                    "pnpm" if (project / "pnpm-lock.yaml").exists()
                    else "yarn" if (project / "yarn.lock").exists()
                    else "npm"
                )
                if target == "build":
                    cmd = [manager, "run", "build"] if (project / "package.json").exists() else [manager, "install"]
                elif target == "test":
                    cmd = [manager, "test"]
                elif target == "lint":
                    cmd = [manager, "run", "lint"]
                elif target == "clean":
                    cmd = [manager, "run", "clean"]
                else:
                    cmd = [manager, "run", target]
            elif "make" in detected:
                cmd = [make_command() or "make", target]
            elif "docker" in detected:
                if target == "docker" or target == "build":
                    cmd = ["docker", "build", "-t", project.name, "."]
                elif target == "docker-run":
                    cmd = ["docker", "run", "--rm", project.name]
                else:
                    callback(f"Unknown Docker target: {target}")
                    _finish_task(task_id, "failed")
                    return
            elif "java" in detected or "kotlin" in detected:
                if target not in {"build", "test"}:
                    callback(f"Unknown Java/Kotlin target: {target}")
                    _finish_task(task_id, "failed")
                    return
                gradle_file = any(
                    (project / name).is_file() for name in ("build.gradle", "build.gradle.kts")
                )
                if gradle_file or "kotlin" in detected:
                    cmd = gradle_command(project, "test" if target == "test" else "build")
                elif (project / "pom.xml").is_file():
                    cmd = ["mvn", "test" if target == "test" else "package"]
                else:
                    callback("No Gradle or Maven build file found.")
                    _finish_task(task_id, "failed")
                    return
            else:
                callback(f"No supported project type found for target: {target}")
                _finish_task(task_id, "failed")
                return

            callback(f"Running: {' '.join(cmd)}")
            success = await asyncio.to_thread(CommandExecutor.run, cmd, cwd=path, callback=callback)
            _finish_task(task_id, "completed" if success else "failed")
        except Exception as e:
            logger.exception("make target failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_make_target)
    return {"task_id": task_id}


@app.post("/task/setup-hooks")
async def task_setup_hooks(request: PathRequest, background_tasks: BackgroundTasks) -> Dict[str, str]:
    path = _validate_path(request.path)
    task_id = _new_task(f"Git hooks: {Path(path).name}")

    async def run_setup_hooks() -> None:
        try:
            callback = get_log_callback(task_id)
            project = Path(path)

            if (project / "package.json").exists():
                callback("Installing Husky for Node.js project...")
                # Husky setup
                cmd1 = ["npm", "install", "husky", "--save-dev", "--ignore-scripts"]
                cmd2 = ["npx", "husky", "install"]
                success = await asyncio.to_thread(CommandExecutor.run, cmd1, cwd=path, callback=callback)
                if success:
                    success = await asyncio.to_thread(CommandExecutor.run, cmd2, cwd=path, callback=callback)
            elif (project / "requirements.txt").exists():
                callback("Installing pre-commit for Python project...")
                # pre-commit setup
                venv_python = _project_python(project)
                if venv_python is None:
                    callback("No project virtual environment found. Run the Python setup first.")
                    _finish_task(task_id, "failed")
                    return
                cmd1 = [str(venv_python), "-m", "pip", "install", "pre-commit"]
                cmd2 = [str(venv_python), "-m", "pre_commit", "install"]
                success = await asyncio.to_thread(CommandExecutor.run, cmd1, cwd=path, callback=callback)
                if success:
                    success = await asyncio.to_thread(CommandExecutor.run, cmd2, cwd=path, callback=callback)
            else:
                callback("No supported project type found for hooks installation.")
                _finish_task(task_id, "failed")
                return

            _finish_task(task_id, "completed" if success else "failed")
        except Exception as e:
            logger.exception("task_setup_hooks failed")
            get_log_callback(task_id)(f"Error: {e}")
            _finish_task(task_id, "failed")

    background_tasks.add_task(run_setup_hooks)
    return {"task_id": task_id}


@app.get("/ai/status")
async def ai_status() -> Dict[str, Any]:
    """Report Ollama connectivity and the models available to the assistant."""
    client = OllamaClient()
    status = await asyncio.to_thread(client.status)
    status["quick_prompts"] = client.quick_prompts()
    return status


@app.post("/ai/generate-setup")
async def generate_setup(request: GenerateSetupRequest) -> List[Dict[str, str]]:
    """Analyze project configuration files and generate a JSON array of setup commands."""
    path_str = _validate_path(request.path)
    project_path = Path(path_str)
    reply = "No reply"  # Initialize to avoid locals() hack in exception handler

    try:
        # Collect context from key files
        context_files = ['README.md', 'package.json', 'requirements.txt', 'Gemfile', 'Cargo.toml']
        context_data = []

        def read_context():
            # Links that point outside the project, and big files, stay out.
            found = []
            for filename in context_files:
                text = read_text_inside(project_path, filename, 50 * 1024)
                if text is None:
                    continue
                found.append(f"--- {filename} ---\n{text}")
            return "\n\n".join(found)

        context = await asyncio.to_thread(read_context)

        client = OllamaClient()
        prompt = (
            "Analyze the following project configuration files and generate a precise list of "
            "setup commands required to get the project running from a clean state.\n\n"
            f"{untrusted_block(context)}\n\n"
            "Return ONLY a valid JSON array of objects with 'command' and 'description' keys. "
            "Do not include any conversational text or markdown code fences. "
            "Example format: [{\"command\": \"npm install\", \"description\": \"Install dependencies\"}]"
        )

        # Use the chat method to get the response
        result = await asyncio.to_thread(
            client.chat,
            messages=[
                {'role': 'system', 'content': client.SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ]
        )

        reply = result['reply'].strip()

        # Clean potential markdown fences
        if reply.startswith("```"):
            # Handle ```json ... ``` or ``` ... ```
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", reply, re.DOTALL)
            if match:
                reply = match.group(1)

        parsed = json.loads(reply)
        if not isinstance(parsed, list):
            raise ValueError("AI response was not a JSON array")
        steps = []
        for item in parsed[:30]:
            if not isinstance(item, dict) or not item.get("command"):
                continue
            command = str(item["command"])[:500]
            # The model only suggests; drop anything that looks like malware.
            if command_risk(command):
                logger.warning("AUDIT dropped risky AI setup suggestion: %s", command)
                continue
            steps.append({"command": command, "description": str(item.get("description", ""))[:300]})
        return steps

    except (json.JSONDecodeError, ValueError):
        logger.error("AI generated invalid JSON: %s", reply)
        raise HTTPException(500, "AI returned an invalid JSON format. Please try again.")
    except Exception as e:
        logger.exception("AI setup generation failed")
        raise HTTPException(500, f"Internal error during AI setup generation: {str(e)}")


@app.post("/ai/chat")
async def ai_chat(request: AIChatRequest) -> Dict[str, Any]:
    """Ask the configured Qwen model a question about a project."""
    path = _validate_path(request.path) if request.path else None
    if request.include_context and path is None:
        raise HTTPException(400, "A project path is required when include_context is true")

    client = OllamaClient()
    if request.model and request.model not in client.allowed_models:
        raise HTTPException(400, "The requested Ollama model is not allowed")
    try:
        result = await asyncio.to_thread(
            client.ask_about_project,
            path or os.getcwd(),
            request.message,
            history=[turn.model_dump() for turn in request.history],
            model=request.model,
            temperature=(
                request.temperature if request.temperature is not None else AI_TEMPERATURE
            ),
            max_tokens=request.max_tokens if request.max_tokens is not None else AI_MAX_TOKENS,
            include_context=request.include_context,
        )
    except OllamaError as e:
        logger.warning("ai_chat failed: %s", e)
        raise HTTPException(503, str(e)) from e

    result["path"] = path
    return result


# --------------------------------------------------------------------------- #
#  Task status / management
# --------------------------------------------------------------------------- #
@app.get("/task/{task_id}/logs")
def get_task_logs(task_id: str) -> Dict[str, Any]:
    task = _get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return {"task_id": task_id, "status": task["status"], "logs": task["logs"]}


@app.post("/task/{task_id}/cancel")
def cancel_task(task_id: str) -> Dict[str, str]:
    """Stop a running task: kills its current command (and child processes)."""
    row = get_db().execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Task not found")
    if row["status"] != "running":
        raise HTTPException(409, f"Task is already {row['status']}")
    _cancel_event(task_id).set()
    get_log_callback(task_id)("🛑 Cancel requested…")
    return {"task_id": task_id, "status": "cancelling"}


@app.delete("/task/{task_id}")
def delete_task(task_id: str) -> Dict[str, str]:
    cursor = get_db().execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    get_db().commit()
    if cursor.rowcount == 0:
        raise HTTPException(404, "Task not found")
    return {"task_id": task_id, "status": "deleted"}


# --------------------------------------------------------------------------- #
#  WebSocket log streaming
# --------------------------------------------------------------------------- #
@app.websocket("/ws/logs/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str) -> None:
    headers = websocket.headers
    if (
        not _loopback_peer(websocket.client)
        or not _origin_ok(headers.get("origin"), headers.get("host"))
        or not _token_ok(websocket.query_params.get("token"))
    ):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    last_seq = -1
    miss_ticks = 0

    try:
        while True:
            row = await asyncio.to_thread(
                lambda: get_db().execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            )
            if row is None:
                miss_ticks += 1
                if miss_ticks > 20:
                    await websocket.send_text(f"[error] Task {task_id} not found")
                    await websocket.close(code=1008)
                    return
            else:
                miss_ticks = 0
                new_lines = await asyncio.to_thread(_get_logs, task_id, last_seq)
                if new_lines:
                    await websocket.send_text("\n".join(new_lines))
                    last_seq += len(new_lines)
                if row["status"] in ("completed", "failed", "cancelled") and not new_lines:
                    await websocket.send_text(f"[status] {row['status']}")
                    await websocket.close()
                    return
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket error")
        try:
            await websocket.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Static UI
# --------------------------------------------------------------------------- #
# FIX 3: Added missing opening parenthesis to Path(__file__)
STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/")
async def read_index() -> HTMLResponse:
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return HTMLResponse(
            "<h1>Setup Manager API</h1>"
            "<p>UI not installed. POST to <code>/analyze</code> or see "
            "<a href='/docs'>/docs</a>.</p>"
        )
    page = index.read_text(encoding="utf-8")
    page = page.replace("__SM_TOKEN__", html_escape(API_TOKEN, quote=True))
    return HTMLResponse(page)


def create_launcher_window(port: int):
    try:
        import tkinter as tk
        from tkinter import messagebox
        import webbrowser

        root = tk.Tk()
        root.title("Setup Manager Launcher")
        root.geometry("400x200")

        label = tk.Label(root, text="Setup Manager Server is Running", font=("Arial", 12, "bold"))
        label.pack(pady=20)

        url = f"http://127.0.0.1:{port}"
        info_label = tk.Label(root, text=f"Access the UI at:\n{url}", font=("Arial", 10))
        info_label.pack(pady=10)

        def open_browser():
            webbrowser.open(url)

        btn = tk.Button(root, text="Open Web Interface", command=open_browser, width=20, height=2)
        btn.pack(pady=20)

        root.mainloop()
    except Exception:  # ImportError, or TclError on a headless machine
        print("Launcher window unavailable. Opening browser directly...")
        webbrowser.open(f"http://127.0.0.1:{port}")


# FIX 4: Consolidated the mangled, duplicated, and syntactically invalid `if __name__ == "__main__":` blocks
# into a single, clean entry point that safely handles auto-opening and avoids undefined variables.
if __name__ == "__main__":
    import uvicorn

    from core.launch_args import parse_launch_args

    # The Desktop shortcut starts the app through pythonw.exe, which has no
    # environment block to rely on, so it passes these as arguments.
    options = parse_launch_args(sys.argv[1:])
    if options.port is not None:
        os.environ["SETUP_MANAGER_PORT"] = str(options.port)
    if options.safe_root:
        os.environ["SETUP_MANAGER_SAFE_ROOT"] = options.safe_root

    auto_open = options.auto_open or os.getenv("SETUP_MANAGER_AUTO_OPEN", "false").lower() == "true"
    headless = options.headless or "--no-gui" in sys.argv

    # 8010 is preferred, but the port is not always free: something else may
    # be holding it. In that case we move up to the next free port, unless the
    # holder is a Setup Manager already - then we open that one instead of
    # starting a second.
    pinned = os.getenv("SETUP_MANAGER_PORT")
    try:
        APP_PORT, already_running = choose_port(explicit=int(pinned) if pinned else None)
    except RuntimeError as exc:
        print(f"❌ ERROR: {exc}")
        sys.exit(1)

    if already_running:
        print(f"ℹ️  Setup Manager is already running at {already_running}")
        if auto_open and not headless:
            webbrowser.open(already_running)
        sys.exit(0)

    if pinned is None and APP_PORT != port_range()[0]:
        print(f"⚠️  Port {port_range()[0]} was taken, using {APP_PORT} instead.")

    print(f"🚀 Starting Setup Manager on http://127.0.0.1:{APP_PORT}")
    
    # Open browser or launcher in a separate thread so it doesn't block uvicorn
    if auto_open and not headless:
        threading.Thread(target=create_launcher_window, args=(APP_PORT,), daemon=True).start()
    
    try:
        # Run uvicorn in the main thread. This prevents daemon thread weirdness 
        # and ensures you see all logs directly in the console for debugging.
        uvicorn.run(
            app, 
            host="127.0.0.1", 
            port=APP_PORT, 
            log_level=LOG_LEVEL.lower()
        )
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"❌ ERROR: Port {APP_PORT} is already in use!")
            print("   Fix: Close the other program, or set SETUP_MANAGER_PORT to a different port.")
        else:
            raise