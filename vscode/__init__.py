"""VS Code integration.

Two things live here: a small launcher that opens a project in VS Code, and
the knowledge base of VS Code specific problems in
:mod:`vscode.knowledge_base`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from .knowledge_base import VSCodeKnowledgeBase

__all__ = ["VSCodeKnowledgeBase", "find_vscode", "open_in_vscode", "vscode_path"]


def vscode_path() -> Optional[str]:
    """Return the path to the VS Code executable, if it is installed."""
    found = shutil.which("code")
    if found:
        return found
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft VS Code" / "Code.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def find_vscode() -> bool:
    """True when VS Code is installed on this machine."""
    return vscode_path() is not None


def open_in_vscode(folder: str | Path) -> bool:
    """Open a folder in VS Code. Returns False when VS Code is not installed."""
    executable = vscode_path()
    if not executable:
        return False
    target = Path(folder)
    if not target.is_dir():
        return False
    try:
        subprocess.Popen(
            [executable, str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        return False
    return True
