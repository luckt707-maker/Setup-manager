import asyncio
import base64
import inspect
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
from doctor import (
    BuildDoctor,
    DoctorLogManager,
    KnowledgeBase,
    diagnose_build_output,
    summarize,
)
from ollama_client import OllamaClient, OllamaError

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
app = FastAPI(title="Setup Manager Web API", version=APP_VERSION, redirect_slashes=False)

# --------------------------------------------------------------------------- #
#  Security layer
#  * Host allow-list           -> blocks DNS-rebinding attacks on localhost
#  * Per-session API token     -> blocks drive-by requests from other websites
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


def _origin_ok(origin: Optional[str], host: Optional[str]) -> bool:
    if not origin:
        return True  # non-browser clients (curl, tests) send no Origin
    return origin in {f"http://{host}", f"https://{host}"}


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    
    # ALLOW VS Code extensions (Cline/Qwen) to bypass strict origin checks
    is_vscode = origin and (
        origin.startswith("vscode-webview://") or 
        origin.startswith("http://localhost") or 
        origin.startswith("http://127.0.0.1")
    )
    
    if not is_vscode and not _origin_ok(origin, host):
        logger.warning(f"BLOCKED Cross-origin request: Origin={origin}, Host={host}, Path={path}")
        return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
        
    is_public = path in PUBLIC_PATHS or path.startswith("/static/")
    has_token = secrets.compare_digest(request.headers.get("x-setup-token", ""), API_TOKEN)
    
    # Allow VS Code if it passes the origin check above, OR if it has the valid token
    if not is_public and not is_vscode and not has_token:
        logger.warning(f"BLOCKED Missing token: Path={path}")
        return JSONResponse({"detail": "Missing or invalid API token"}, status_code=401)
        
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; img-src 'self' data:; frame-ancestors 'none'",
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
            str(TASK_DB_PATH), timeout=30, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
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
    if event is not None and event.is_set() and status == "failed":
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

    def __call__(self, message: str) -> None:
        _append_log(self.task_id, message)


def get_log_callback(task_id: str) -> Callable[[str], None]:
    return _TaskLogger(task_id)


def _append_log(task_id: str, message: str) -> None:
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM task_logs WHERE task_id = ?", (task_id,)
    ).fetchone()
    seq = row["n"]
    if seq >= MAX_LOG_LINES:
        if seq == MAX_LOG_LINES:
            db.execute("INSERT INTO task_logs VALUES (?, ?, ?)", (task_id, seq, "[log truncated: line limit reached]"))
            db.commit()
        return
    db.execute("INSERT INTO task_logs VALUES (?, ?, ?)", (task_id, seq, str(message)[:4000]))
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
    """Single allowed projects root. Explicit and fail-closed (no cwd heuristics)."""
    configured = os.getenv("SETUP_MANAGER_SAFE_ROOT")
    default = r"C:\dev" if sys.platform == "win32" else str(Path.home() / "projects")
    return Path(configured or default).expanduser().resolve()


def _validate_safe_path(path: str) -> str:
    """Resolve symlinks/junctions/.. and require the result to be inside the safe root."""
    if not path or not path.strip():
        raise HTTPException(400, "A project path is required")
    if "\x00" in path:
        raise HTTPException(400, "Invalid path")
    resolved = Path(path).expanduser().resolve()
    safe_root = _safe_root()
    if not resolved.is_relative_to(safe_root):
        raise HTTPException(400, f"Security Error: path is outside the allowed root {safe_root}")
    return str(resolved)


def _validate_path(path: str) -> str:
    # 1. Security Check: Path Traversal
    path = _validate_safe_path(path)

    # 2. Existence Check
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise HTTPException(400, f"Path does not exist: {p}")
    if not p.is_dir():
        raise HTTPException(400, f"Path is not a directory: {p}")
    return str(p)


def find_free_port(start_port: int = 8010, host: str = "127.0.0.1") -> int:
    """Finds the first available port starting from start_port."""
    port = start_port
    while port < 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except socket.error:
                port += 1
    raise RuntimeError("No free ports available")


def _send_notification(title: str, message: str) -> None:
    """Best-effort desktop toast. Opt-in (SETUP_MANAGER_NOTIFY=1); never blocks a task."""
    if not NOTIFY:
        return

    def _notify() -> None:
        try:
            system = platform.system()
            if system == "Windows":
                script = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "$n = New-Object System.Windows.Forms.NotifyIcon; "
                    "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
                    f"$n.ShowBalloonTip(5000, '{title}', '{message}', 'Info'); Start-Sleep -Seconds 6; $n.Dispose()"
                )
                subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", script],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif system == "Linux":
                subprocess.Popen(["notify-send", title, message])
            elif system == "Darwin":
                subprocess.Popen(["osascript", "-e", f'display notification "{message}" with title "{title}"'])
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
    path: str


class MountRequest(BaseModel):
    image_path: str


class FileScanRequest(BaseModel):
    path: str


class LaunchRequest(BaseModel):
    path: str
    candidate_index: int = Field(..., ge=0)
    run_mode: Literal["host", "docker"] = "host"


class CustomCommandRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=2000)
    cwd: Optional[str] = None
    confirm: bool = False


class PathRequest(BaseModel):
    path: str = Field(..., min_length=1)


class ExtensionRequest(BaseModel):
    project_type: str
    ext_types: Optional[List[str]] = None


class MakefileRequest(BaseModel):
    action: str = Field(..., pattern="^(template|url)$")
    url: Optional[str] = None


class AIMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1)


class AIChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    path: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(None, ge=1, le=8192)
    include_context: bool = True
    history: List[AIMessage] = Field(default_factory=list)


class GenerateSetupRequest(BaseModel):
    path: str


# --------------------------------------------------------------------------- #
#  System Health
# --------------------------------------------------------------------------- #
@app.get("/system/doctor")
def system_doctor() -> Dict[str, Any]:
    """Check for essential binaries on the system."""
    tools_to_check = {
        "python": "https://www.python.org/downloads/",
        "node": "https://nodejs.org/",
        "npm": "https://nodejs.org/",
        "git": "https://git-scm.com/downloads",
        "docker": "https://www.docker.com/products/docker-desktop",
    }
    results = {}
    for tool, url in tools_to_check.items():
        path = shutil.which(tool)
        results[tool] = {
            "status": "ready" if path else "missing",
            "url": url if not path else None,
            "path": path
        }
    return {"tools": results}

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
#  Health / task listing
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> Dict[str, Any]:
    count = get_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    return {"status": "ok", "version": APP_VERSION, "tasks": count}


@app.get("/api/config")
def api_config() -> Dict[str, Any]:
    return {
        "version": APP_VERSION,
        "safe_root": str(_safe_root()),
        "allow_shell": ALLOW_SHELL,
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
    }


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
@app.post("/analyze")
async def analyze_folder(request: AnalyzeRequest) -> Dict[str, Any]:
    path = _validate_path(request.path)
    try:
        return await asyncio.to_thread(
            lambda: ProjectOrchestrator(path).analyze()
        )
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
        result["malware_scan"] = await asyncio.to_thread(_scan_with_defender, path)
        return result
    except Exception as e:
        logger.exception("security scan failed")
        raise HTTPException(400, str(e)) from e


@app.post("/file-safety-scan")
async def file_safety_scan(request: FileScanRequest) -> Dict[str, Any]:
    path = Path(request.path).expanduser().resolve()
    if not path.is_file():
        raise HTTPException(400, "Choose an existing downloaded file to scan")
    return await asyncio.to_thread(_scan_with_defender, path)


@app.post("/diagnose")
async def diagnose_project(request: AnalyzeRequest) -> Dict[str, Any]:
    path = _validate_path(request.path)
    try:
        return await asyncio.to_thread(ProjectOrchestrator(path).diagnose)
    except Exception as e:
        logger.exception("project diagnosis failed")
        raise HTTPException(400, str(e)) from e


@app.post("/task/install-dependencies/{project_type}")
async def install_dependencies(
    project_type: str,
    background_tasks: BackgroundTasks,
    path: str = Query(..., description="Selected project directory"),
) -> Dict[str, Any]:
    path = _validate_path(path)
    project = Path(path)
    pre_commands: List[List[str]] = []
    if project_type == "python" and (project / "requirements.txt").is_file():
        venv_python = _project_python(project)
        if venv_python is None:  # never install into Setup Manager's own environment
            pre_commands.append([sys.executable, "-m", "venv", str(project / "venv")])
            venv_python = project / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        command = [str(venv_python), "-m", "pip", "install", "--only-binary=:all:", "-r", "requirements.txt"]
    elif project_type == "nodejs" and (project / "package.json").is_file():
        manager = (
            "pnpm" if (project / "pnpm-lock.yaml").exists()
            else "yarn" if (project / "yarn.lock").exists()
            else "npm"
        )
        if shutil.which(manager) is None:
            raise HTTPException(400, f"{manager} is required but was not found on PATH")
        command = [manager, "install", "--ignore-scripts"]
    elif project_type == "dotnet" and (
        any(project.glob("*.csproj")) or any(project.glob("*.sln"))
    ):
        if shutil.which("dotnet") is None:
            raise HTTPException(400, ".NET SDK is required but was not found on PATH")
        command = ["dotnet", "restore"]
    else:
        raise HTTPException(400, "No supported dependency manifest found for this project type")

    preflight = await asyncio.to_thread(_scan_with_defender, project)
    _enforce_defender_result(preflight, project)

    task_id = _new_task(f"Install {project_type} dependencies: {project.name}")

    async def run_install() -> None:
        try:
            success = True
            for pre in pre_commands:
                success = success and await asyncio.to_thread(
                    CommandExecutor.run, pre, cwd=path, callback=get_log_callback(task_id)
                )
            success = success and await asyncio.to_thread(
                CommandExecutor.run,
                command,
                cwd=path,
                callback=get_log_callback(task_id),
            )
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

# --------------------------------------------------------------------------- #
#  Project Launch
# --------------------------------------------------------------------------- #
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
    task_id = _new_task(f"Launch: {candidate['label']}")

    # Determine Docker image based on candidate label or project type
    image = None
    if request.run_mode == "docker":
        detector = ProjectDetector(path)
        detected = detector.detect()
        if "python" in detected: image = CommandExecutor.IMAGE_MAPPING["python"]
        elif "nodejs" in detected: image = CommandExecutor.IMAGE_MAPPING["nodejs"]
        elif "dotnet" in detected: image = CommandExecutor.IMAGE_MAPPING["dotnet"]
        else: image = CommandExecutor.IMAGE_MAPPING["general"]

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
                image=image,
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
    return await asyncio.to_thread(_mount_disk_image, request.image_path)


@app.post("/dismount")
async def dismount_image(request: MountRequest) -> Dict[str, Any]:
    return await asyncio.to_thread(_mount_disk_image, request.image_path, True)


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
@app.post("/task/checks")
async def run_checks(
    background_tasks: BackgroundTasks,
    project_type: str = Query(...),
    path: str = Query(...),
) -> Dict[str, str]:
    path = _validate_path(path)
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
            _finish_task(task_id, "completed", results=results)
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
    cwd = _validate_path(request.cwd or str(_safe_root()))
    logger.warning("AUDIT custom command (shell=%s) in %s: %s", ALLOW_SHELL, cwd, request.command)
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
            found = []
            for filename in context_files:
                file_path = project_path / filename
                if file_path.is_file():
                    try:
                        found.append(f"--- {filename} ---\n{file_path.read_text(encoding='utf-8', errors='replace')}")
                    except Exception as e:
                        found.append(f"--- {filename} --- (Error reading: {e})")
            return "\n\n".join(found)

        context = await asyncio.to_thread(read_context)

        client = OllamaClient()
        prompt = (
            "Analyze the following project configuration files and generate a precise list of "
            "setup commands required to get the project running from a clean state.\n\n"
            f"Context:\n{context}\n\n"
            "Return ONLY a valid JSON array of objects with 'command' and 'description' keys. "
            "Do not include any conversational text or markdown code fences. "
            "Example format: [{\"command\": \"npm install\", \"description\": \"Install dependencies\"}]"
        )

        # Use the chat method to get the response
        result = await asyncio.to_thread(
            client.chat,
            messages=[{'role': 'user', 'content': prompt}]
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
        return [
            {"command": str(item["command"])[:500], "description": str(item.get("description", ""))[:300]}
            for item in parsed[:30]
            if isinstance(item, dict) and item.get("command")
        ]

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
        not _origin_ok(headers.get("origin"), headers.get("host"))
        or not secrets.compare_digest(websocket.query_params.get("token", ""), API_TOKEN)
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
    html = index.read_text(encoding="utf-8").replace("__SM_TOKEN__", API_TOKEN)
    return HTMLResponse(html)


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

    # The port defaults to 8010 so the VS Code extension always knows where to
    # connect, but SETUP_MANAGER_PORT can move it (useful when 8010 is taken).
    APP_PORT = int(os.getenv("SETUP_MANAGER_PORT", "8010"))

    auto_open = os.getenv("SETUP_MANAGER_AUTO_OPEN", "false").lower() == "true"
    headless = "--no-gui" in sys.argv

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
            print("   Fix: Close any other Setup Manager windows, or run 'taskkill /F /IM python.exe' in terminal.")
        else:
            raise