"""Reading the ways a project can be run.

A Makefile usually has a dozen targets and a compose file a few services.
Offering "make" on its own leaves the user to know which target they want,
so the app reads both files and offers the real choices.

Standard library only, like the rest of ``core`` - the compose file is
parsed here rather than with PyYAML, because the app has no third-party
dependencies outside its own environment.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import List, Optional

#: The names make looks for, in the order it looks for them.
MAKEFILE_NAMES = ("Makefile", "makefile", "GNUmakefile")

#: The names the make program itself goes by, in the order they are tried.
MAKE_COMMANDS = ("make", "mingw32-make", "gmake")

#: The names docker compose looks for.
COMPOSE_NAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

#: A target line: names, then a colon. The names may be several.
_TARGET_RE = re.compile(r"^([A-Za-z0-9_./$(){}%+-][A-Za-z0-9_./$(){}%+\- ]*):(?!=)(.*)$")

#: A service line inside a compose file: two spaces of indent, then a name.
_SERVICE_RE = re.compile(r"^(\s{2,})([A-Za-z0-9_.-]+):(\s|$)")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def make_command() -> Optional[str]:
    """The make program that is installed, or None when there is none.

    The name differs by machine: 'make' on Linux and macOS, 'mingw32-make'
    for MSYS2 on Windows. A button that names a program that is not there
    only fails later, so the name is checked before it is offered.
    """
    for name in MAKE_COMMANDS:
        if shutil.which(name):
            return name
    return None


def find_makefile(root: Path) -> Optional[Path]:
    """The Makefile in a folder, if there is one."""
    root = Path(root)
    for name in MAKEFILE_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def make_targets(root: Path) -> List[str]:
    """The targets a Makefile offers, in the order the file lists them.

    Pattern rules ('%.o: %.c') and internal targets ('.PHONY') are left
    out: they are not things a user picks.
    """
    path = find_makefile(root)
    if path is None:
        return []

    targets: List[str] = []
    for line in _read(path).splitlines():
        if not line.strip() or line[0].isspace():
            # A recipe line belongs to the target above it.
            continue
        if line.lstrip().startswith("#"):
            continue
        if "=" in line.split(":", 1)[0]:
            # A variable assignment, not a target.
            continue
        match = _TARGET_RE.match(line)
        if not match:
            continue
        for name in match.group(1).split():
            if not name or name.startswith(".") or "%" in name:
                continue
            if name not in targets:
                targets.append(name)
    return targets


def find_compose_file(root: Path) -> Optional[Path]:
    """The compose file in a folder, if there is one."""
    root = Path(root)
    for name in COMPOSE_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def compose_services(root: Path) -> List[str]:
    """The services a compose file declares.

    Only the service names are read, so a small reader is enough: the
    ``services:`` block and the names one level inside it.
    """
    path = find_compose_file(root)
    if path is None:
        return []

    services: List[str] = []
    in_services = False
    services_indent = 0
    for line in _read(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if not in_services:
            if stripped == "services:" or stripped.startswith("services:"):
                in_services = True
                services_indent = indent
            continue
        if indent <= services_indent:
            # The services block is over.
            break
        match = _SERVICE_RE.match(line)
        if match and len(match.group(1)) == services_indent + 2:
            name = match.group(2)
            if name not in services:
                services.append(name)
    return services
