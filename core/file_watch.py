"""Warn when a sneaky file appears or a program starts from a watched folder.

The watch is off until the user starts it, and it stops by itself when
the page stops checking on it or after a time limit. The first look lists
files and programs that can already run. Later looks report only what
is new. Windows Defender still blocks known threats. This does not
read file contents and it does not run the file.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid

logger = logging.getLogger("setup_manager.file_watch")

RISKY_EXTENSIONS = {
    ".exe", ".scr", ".pif", ".com", ".bat", ".cmd", ".js", ".jse",
    ".vbs", ".vbe", ".wsf", ".wsh", ".hta", ".msi", ".msp", ".iso",
    ".img", ".lnk", ".ps1", ".jar", ".cpl", ".reg", ".msc", ".application",
    ".py",
}

# A document-looking name in front of a risky ending, as in invoice.pdf.exe.
DISGUISED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt", ".jpg", ".jpeg",
    ".png", ".gif", ".zip", ".mp3", ".mp4", ".csv", ".rtf",
}

QUIET_NAMES = {"desktop.ini", "thumbs.db"}
RIGHT_TO_LEFT = "\u202e"
_TOKEN = re.compile(r'"([^"]+)"|(\S+)')
_CREATE_NO_WINDOW = 0x08000000


def watched_folders() -> List[Path]:
    """Folders a downloaded or surprise program usually lands in."""
    home = Path.home()
    public = os.environ.get("PUBLIC", r"C:\Users\Public")
    folders = [home / "Downloads", home / "Desktop", Path(public) / "Desktop"]
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        folders.append(Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup")
    return folders


def _place(path: Path) -> str:
    text = str(path).lower()
    if "\\startup" in text:
        return "startup"
    if "\\downloads" in text:
        return "downloads"
    if "\\desktop" in text:
        return "desktop"
    return "other"


def _inside(path: str, folders: Iterable[Path]) -> bool:
    try:
        candidate = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for folder in folders:
        try:
            candidate.relative_to(Path(folder).resolve())
        except (OSError, ValueError):
            continue
        else:
            return True
    return False


def _alert(kind: str, title: str, where: str, detail: str) -> Dict[str, str]:
    return {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "title": title,
        "where": where,
        "detail": detail,
    }


def describe_file(path: Path) -> Optional[Dict[str, str]]:
    """Return an alert when a new file in a watched folder can run."""
    name = path.name
    if not name or name.lower() in QUIET_NAMES:
        return None
    place = _place(path)
    where = str(path)
    if RIGHT_TO_LEFT in name:
        return _alert(
            "file", name, where,
            f"{name} hides its real ending with a special character. Don't open it.",
        )
    suffixes = [part.lower() for part in path.suffixes]
    risky = bool(suffixes) and suffixes[-1] in RISKY_EXTENSIONS
    # A shortcut on the Desktop is usually one the user just made.
    # The last ending is what matters: "Print 7.3.lnk" is still a shortcut.
    if place == "desktop" and suffixes and suffixes[-1] == ".lnk":
        return None
    if place == "startup" and (risky or suffixes == [".url"]):
        return _alert(
            "file", name, where,
            f"{name} was added to the Startup folder. It can run the next time you sign in.",
        )
    if len(suffixes) >= 2 and suffixes[-1] in RISKY_EXTENSIONS and suffixes[-2] in DISGUISED_EXTENSIONS:
        return _alert(
            "file", name, where,
            f"{name} looks like a document, but it can run a program. Open it only if you were expecting it.",
        )
    if risky:
        return _alert(
            "file", name, where,
            f"{name} can run a program. Open it only if you were expecting it.",
        )
    return None


def _command_target(command: str, folders: Iterable[Path]) -> str:
    """A risky file named on a command line, when it sits in a watched folder."""
    for match in _TOKEN.finditer(command or ""):
        token = match.group(1) or match.group(2) or ""
        if not token or not _inside(token, folders):
            continue
        if Path(token).suffix.lower() in RISKY_EXTENSIONS:
            return token
    return ""


def describe_process(process: Dict[str, object], folders: Iterable[Path]) -> Optional[Dict[str, str]]:
    """Return an alert when a new program starts from a watched folder."""
    name = str(process.get("name") or "A program")
    executable = str(process.get("executable") or "")
    command = str(process.get("command") or "")
    target = executable if executable and _inside(executable, folders) else ""
    if not target:
        target = _command_target(command, folders)
    if not target:
        return None
    leaf = Path(target).name or name
    place = _place(Path(target))
    if place == "startup":
        detail = f"{leaf} started from the Startup folder. If you did not open it, close it."
    elif place == "desktop":
        detail = f"{leaf} started from the Desktop. If you did not open it, close it."
    elif place == "downloads":
        detail = f"{leaf} started from Downloads. If you did not open it, close it."
    else:
        detail = f"{leaf} started from a watched folder. If you did not open it, close it."
    return _alert("program", leaf, target, detail)


class FileWatch:
    """Remember the first look, then report only what shows up after it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen_files = set()
        self._seen_programs = set()
        self._baselined = False
        self._alerts: List[Dict[str, str]] = []
        self._last_processes: List[Dict[str, object]] = []

    def note(
        self,
        files: Iterable[Path],
        processes: Iterable[Dict[str, object]],
        folders: Iterable[Path],
    ) -> List[Dict[str, str]]:
        """Compare this look with the last one.

        The first look reports what can already run. Later looks report
        only a file or program that was not in the previous look.
        """
        folder_list = list(folders)
        fresh: List[Dict[str, str]] = []
        when = "new" if self._baselined else "already"
        file_keys = []
        for path in files:
            key = str(path).lower()
            file_keys.append(key)
            if self._baselined and key in self._seen_files:
                continue
            found = describe_file(path)
            if found:
                found["when"] = when
                fresh.append(found)
        program_keys = []
        for process in processes:
            pid = str(process.get("pid") or "")
            executable = str(process.get("executable") or "")
            if not pid:
                continue
            key = pid + "|" + executable.lower()
            program_keys.append(key)
            if self._baselined and key in self._seen_programs:
                continue
            found = describe_process(process, folder_list)
            if found:
                found["when"] = when
                fresh.append(found)
        with self._lock:
            self._seen_files = set(file_keys)
            self._seen_programs = set(program_keys)
            self._baselined = True
            self._alerts.extend(fresh)
            self._alerts = self._alerts[-20:]
        return fresh

    def add_note(self, title: str, detail: str) -> None:
        """Put a plain note, such as "stopped automatically", in the list."""
        note = _alert("note", title, "", detail)
        note["when"] = "stopped"
        with self._lock:
            self._alerts.append(note)
            self._alerts = self._alerts[-20:]

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            if not self._baselined:
                summary = "Looking once at what is already here."
            elif any(item.get("when") == "new" for item in self._alerts):
                summary = "Something new can run a program. Windows Defender may still be quiet about it."
            elif self._alerts:
                summary = "These can run a program. They were already here when Setup Manager opened."
            else:
                summary = "Watching while Setup Manager is open. Nothing that can run a program is in the watched folders."
            return {
                "watching": True,
                "summary": summary,
                "alerts": list(self._alerts),
            }

    def poll(self) -> List[Dict[str, str]]:
        folders = watched_folders()
        files = _list_files(folders)
        processes = _list_processes()
        if processes is None:
            processes = self._last_processes
        else:
            self._last_processes = processes
        if files is None:
            files = []
        return self.note(files, processes, folders)


def _list_files(folders: Iterable[Path]) -> Optional[List[Path]]:
    found = []
    saw_a_folder = False
    for folder in folders:
        try:
            if not folder.is_dir():
                continue
            saw_a_folder = True
            entries = list(folder.iterdir())
        except OSError:
            continue
        found.extend(entry for entry in entries if entry.is_file())
    if not saw_a_folder:
        return None
    return found


def _decode_output(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _powershell() -> str:
    """Windows PowerShell by full path, so a same-named program in the app
    folder or the current folder cannot be run in its place."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _list_processes() -> Optional[List[Dict[str, object]]]:
    """Running programs, or None when the list could not be read."""
    if os.name != "nt":
        return []
    command = [
        _powershell(), "-NoProfile", "-NonInteractive", "-Command",
        "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress -Depth 2",
    ]
    kwargs = {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        completed = subprocess.run(
            command, capture_output=True, text=False, timeout=25, check=False, **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = _decode_output(completed.stdout or b"").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    rows = data if isinstance(data, list) else [data]
    processes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        processes.append({
            "pid": row.get("ProcessId") or "",
            "name": row.get("Name") or "",
            "executable": row.get("ExecutablePath") or "",
            "command": row.get("CommandLine") or "",
        })
    return processes


POLL_SECONDS = 8
IDLE_SECONDS = 60
DEFAULT_MAX_MINUTES = 120


def watch_disabled() -> bool:
    """SETUP_MANAGER_FILE_WATCH=0 turns the watch off for good."""
    return os.getenv("SETUP_MANAGER_FILE_WATCH", "1").strip() == "0"


def max_run_seconds() -> float:
    """Longest a watch may run, from SETUP_MANAGER_FILE_WATCH_MAX_MINUTES."""
    raw = os.getenv("SETUP_MANAGER_FILE_WATCH_MAX_MINUTES", "")
    try:
        minutes = float(raw) if raw.strip() else DEFAULT_MAX_MINUTES
    except ValueError:
        minutes = DEFAULT_MAX_MINUTES
    return max(minutes, 0.05) * 60


class WatchDisabled(RuntimeError):
    pass


class _Run:
    def __init__(self, clock: Callable[[], float], max_seconds: float) -> None:
        self.stop = threading.Event()
        self.watch = FileWatch()
        self.started = clock()
        self.last_seen = self.started
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.max_seconds = max_seconds
        self.thread: Optional[threading.Thread] = None


class WatchRunner:
    """Starts and stops the watch. Nothing runs, and no PowerShell is
    started, until start() is called.

    Each run has its own stop Event, so stop() ends the loop at once
    instead of after the next sleep. The loop also stops itself when the
    page has not asked for news in IDLE_SECONDS (closed or hidden page),
    or when it has run for the maximum time.
    """

    def __init__(
        self,
        poll: Optional[Callable[[FileWatch], List[Dict[str, str]]]] = None,
        clock: Callable[[], float] = time.monotonic,
        interval: float = POLL_SECONDS,
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        self._poll = poll or (lambda watch: watch.poll())
        self._clock = clock
        self.interval = interval
        self.idle_seconds = idle_seconds
        self._lock = threading.Lock()
        self._run: Optional[_Run] = None
        self._last: Optional[_Run] = None
        self._stopped_reason = ""
        self._stopped_at = ""
        self._auto_stopped = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._run is not None

    def start(self) -> Dict[str, object]:
        if watch_disabled():
            raise WatchDisabled(
                "The file watch is turned off on this PC (SETUP_MANAGER_FILE_WATCH=0). "
                "Remove that setting to use it."
            )
        with self._lock:
            if self._run is None:
                run = _Run(self._clock, max_run_seconds())
                run.thread = threading.Thread(target=self._loop, args=(run,), name="file-watch", daemon=True)
                self._run = run
                self._last = run
                self._stopped_reason = ""
                self._stopped_at = ""
                self._auto_stopped = False
                run.thread.start()
                logger.info("File watch started")
        return self.status(touch=False)

    def stop(self, reason: str = "You stopped the watch.") -> Dict[str, object]:
        with self._lock:
            run = self._run
        if run is not None:
            self._end(run, reason, auto=False)
        return self.status(touch=False)

    def _end(self, run: _Run, reason: str, auto: bool) -> None:
        run.stop.set()
        with self._lock:
            if self._run is not run:
                return
            self._run = None
            self._stopped_reason = reason
            self._stopped_at = datetime.now(timezone.utc).isoformat()
            self._auto_stopped = auto
        if auto:
            run.watch.add_note("The file watch stopped automatically", reason)
        logger.info("File watch stopped: %s", reason)

    def _loop(self, run: _Run) -> None:
        while not run.stop.is_set():
            now = self._clock()
            if now - run.started >= run.max_seconds:
                minutes = round(run.max_seconds / 60)
                self._end(run, f"It ran for its limit of {minutes} minutes, so it stopped to keep your PC fast.", auto=True)
                return
            if now - run.last_seen >= self.idle_seconds:
                self._end(run, "The Setup Manager page was closed or hidden, so the watch stopped to keep your PC fast.", auto=True)
                return
            try:
                fresh = self._poll(run.watch)
            except Exception:  # noqa: BLE001 - the watch must never crash the app
                logger.exception("file watch failed")
                fresh = []
            if run.stop.is_set():
                return
            for item in fresh or []:
                logger.warning("Sneaky file watch: %s - %s", item.get("title"), item.get("detail"))
            if run.stop.wait(self.interval):
                return

    def join(self, timeout: float = 5) -> None:
        """Wait for the last run's thread (tests and shutdown)."""
        with self._lock:
            run = self._last
        if run is not None and run.thread is not None:
            run.thread.join(timeout)

    def status(self, touch: bool = True) -> Dict[str, object]:
        """What the page shows. A read while running counts as a heartbeat."""
        with self._lock:
            run = self._run
            if run is not None and touch:
                run.last_seen = self._clock()
            shown = run or self._last
            reason, stopped_at, auto = self._stopped_reason, self._stopped_at, self._auto_stopped
        report = shown.watch.snapshot() if shown is not None else {"summary": "", "alerts": []}
        disabled = watch_disabled()
        if run is not None:
            running_for = max(0.0, self._clock() - run.started)
        else:
            running_for = 0.0
            if disabled:
                report["summary"] = "The file watch is turned off on this PC (SETUP_MANAGER_FILE_WATCH=0)."
            elif auto:
                report["summary"] = "Stopped automatically. " + reason
            else:
                report["summary"] = "The file watch is off. It uses no CPU until you start it."
        report.update({
            "watching": run is not None,
            "running": run is not None,
            "disabled": disabled,
            "started_at": run.started_at if run is not None else None,
            "running_seconds": int(running_for),
            "max_minutes": round((run.max_seconds if run is not None else max_run_seconds()) / 60, 2),
            "interval_seconds": self.interval,
            "idle_seconds": self.idle_seconds,
            "stopped_at": stopped_at or None,
            "stopped_reason": reason or None,
            "auto_stopped": auto,
        })
        return report


watch = FileWatch()
runner = WatchRunner()
