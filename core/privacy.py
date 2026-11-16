"""Look on this PC for known tracking programs and remote-control tools.

The check never takes a person's name. It only compares program names
already on this computer with a short public list.
"""

from pathlib import Path
from typing import Dict, Iterable, List
import csv
import io
import os
import subprocess


# Commercial products sold to watch someone's phone or computer.
TRACKERS = {
    "mspy": "mSpy",
    "flexispy": "FlexiSPY",
    "spyzie": "Spyzie",
    "cocospy": "Cocospy",
    "hoverwatch": "Hoverwatch",
    "mobistealth": "Mobistealth",
    "xnspy": "XNSPY",
    "thetruthspy": "TheTruthSpy",
    "spyera": "Spyera",
    "highster": "Highster Mobile",
    "pctattletale": "pcTattletale",
    "webwatcher": "WebWatcher",
    "sniperspy": "SniperSpy",
    "spytech": "Spytech",
    "realtimespy": "RealTimeSpy",
}

# Legitimate remote-control tools. Worth a note, because they also let
# another person see this screen when they are left running.
REMOTE_TOOLS = {
    "anydesk": "AnyDesk",
    "teamviewer": "TeamViewer",
    "rustdesk": "RustDesk",
    "splashtop": "Splashtop",
    "winvnc": "TightVNC or a VNC server",
    "tvnserver": "TightVNC",
    "ultravnc": "UltraVNC",
    "radmin": "Radmin",
    "ammyy": "Ammyy Admin",
    "supremo": "Supremo",
    "dwagent": "DWAgent",
    "screenconnect": "ConnectWise ScreenConnect",
    "connectwisecontrol": "ConnectWise ScreenConnect",
    "logmein": "LogMeIn",
    "ultraviewer": "UltraViewer",
    "remotepc": "RemotePC",
    "atera": "Atera",
}

CHECKLIST = [
    "This check only sees programs on this PC. An app installed only on a phone will not appear here.",
    "Search your own name and email address. You are looking at what a stranger can already open.",
    "Open haveibeenpwned.com and check your own email. If a breach lists it, change that password.",
    "On the phone, review location sharing and any app with device-admin or accessibility access.",
    "Turn on a screen lock, and two-factor sign-in for email.",
]


def _stem(name: str) -> str:
    text = str(name).strip().strip('"')
    if not text:
        return ""
    leaf = text.replace("/", "\\").split("\\")[-1]
    return Path(leaf).stem.lower()


def classify(processes: Iterable[str], startups: Iterable[str], tasks: Iterable[str]) -> Dict[str, object]:
    """Match names the computer already has. No network lookup."""
    findings: List[Dict[str, str]] = []
    seen = set()

    def add(kind: str, status: str, where: str, raw: str, label: str, detail: str) -> None:
        key = (kind, label, where)
        if key in seen:
            return
        seen.add(key)
        findings.append({
            "kind": kind,
            "status": status,
            "where": where,
            "name": raw,
            "title": label,
            "detail": detail,
        })

    groups = (
        ("running", processes),
        ("startup", startups),
        ("scheduled task", tasks),
    )
    for where, names in groups:
        for raw in names:
            stem = _stem(raw)
            if not stem:
                continue
            if stem in TRACKERS:
                add(
                    "tracker", "warn", where, raw, TRACKERS[stem],
                    f"{TRACKERS[stem]} is {where}. That product is sold to watch a person. "
                    "If you did not install it, remove it and change the passwords for this PC.",
                )
            elif stem in REMOTE_TOOLS:
                add(
                    "remote", "note", where, raw, REMOTE_TOOLS[stem],
                    f"{REMOTE_TOOLS[stem]} is {where}. It can show this screen to someone else. "
                    "That is expected only if you installed it.",
                )

    if any(item["kind"] == "tracker" for item in findings):
        summary = "A known tracking program is on this PC."
    elif findings:
        summary = "A remote-control program is on this PC. It is a problem only if you did not install it."
    else:
        summary = "No known tracking program or remote-control tool showed up on this PC."
    return {"summary": summary, "findings": findings, "checklist": list(CHECKLIST)}


def system_tool(name: str) -> str:
    """Full path of a Windows tool in %SystemRoot%\\System32.

    A bare name would let Windows pick a same-named program from the app
    folder or the current folder before System32.
    """
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return str(Path(root) / "System32" / f"{name}.exe")


def _run(command: List[str], timeout: int = 20) -> str:
    if not Path(command[0]).is_file():
        return ""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout or ""


def _process_names() -> List[str]:
    if os.name != "nt":
        return []
    rows = csv.reader(io.StringIO(_run([system_tool("tasklist"), "/FO", "CSV", "/NH"])))
    return [row[0] for row in rows if row]


def _startup_names() -> List[str]:
    names: List[str] = []
    folders = []
    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("PROGRAMDATA", "")
    if appdata:
        folders.append(Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup")
    if programdata:
        folders.append(Path(programdata) / "Microsoft/Windows/Start Menu/Programs/StartUp")
    for folder in folders:
        try:
            entries = list(folder.iterdir()) if folder.is_dir() else []
        except OSError:
            entries = []
        names.extend(entry.name for entry in entries)
    if os.name == "nt":
        text = _run([system_tool("reg"), "query", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"])
        for line in text.splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 3 and parts[1].startswith("REG_"):
                names.append(parts[0])
                names.append(parts[2])
    return names


def _task_names() -> List[str]:
    if os.name != "nt":
        return []
    text = _run([system_tool("schtasks"), "/Query", "/FO", "CSV", "/NH"], timeout=25)
    names = []
    for row in csv.reader(io.StringIO(text)):
        if row:
            names.append(row[0])
    return names


def check_this_pc() -> Dict[str, object]:
    """Read this PC's programs, startup items, and scheduled tasks."""
    return classify(_process_names(), _startup_names(), _task_names())
