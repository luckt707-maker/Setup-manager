"""Health checks for the machine and for project build output.

Two kinds of checks live here:

* **Machine checks** - are the tools Setup Manager relies on installed, is the
  Python environment sane, is ``/etc/wsl.conf`` valid, has Windows dropped
  ``Zone.Identifier`` metadata into the source tree?
* **Build checks** - given the output of a failed build, match the error text
  against the knowledge base so the user gets a named cause and a fix instead
  of a wall of compiler output.

Every check returns :class:`Diagnostic` objects so both the CLI and the web
layer can render the same results.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .knowledge_base import KnowledgeBase

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: Tools the system check knows how to find. A missing tool is an error only
#: when the selected project needs it; see ``required_tools``.
EXPECTED_TOOLS: Dict[str, str] = {
    "python": "https://www.python.org/downloads/",
    "node": "https://nodejs.org/",
    "npm": "https://nodejs.org/",
    "git": "https://git-scm.com/downloads",
    "docker": "https://www.docker.com/products/docker-desktop",
    "make": "https://www.gnu.org/software/make/",
}
_EXTRA_TOOL_URLS: Dict[str, str] = {
    "go": "https://go.dev/dl/",
    "yarn": "https://yarnpkg.com/",
    "pnpm": "https://pnpm.io/",
}
_PYTHON_MANIFESTS = ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile", "setup.cfg")
_NODE_SOURCE = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
_SKIP_WALK = {"node_modules", "venv", ".venv", ".git", "__pycache__", "dist", "build", ".tox"}
# A child folder with its own manifest is a separate program. Its source
# must not make the parent look like it needs that language.
_NESTED_MARKERS = (
    "package.json", "go.mod", "pyproject.toml", "requirements.txt", "Pipfile",
    "Cargo.toml", "composer.json", "Gemfile", "pom.xml", "build.gradle",
    "build.gradle.kts", "Package.swift", "setup.py",
)

#: A ``wsl.conf`` line is ``key = value``; this catches the common breakage
#: where the ``=`` went missing during an edit.
_WSL_CONF_LINE = re.compile(r"^\s*(\w+)\s*=\s*(.*)$")


@dataclass
class Diagnostic:
    """One finding, with enough context to explain and optionally fix it."""

    id: str
    title: str
    status: str
    detail: str = ""
    fix: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_problem(self) -> bool:
        return self.status in (WARN, FAIL)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
            "data": self.data,
        }


# --------------------------------------------------------------------------- #
# Machine checks
# --------------------------------------------------------------------------- #
def _has_source(project: Path, suffixes: set) -> bool:
    """True when the tree contains a source file of one of these types.

    Dependency and environment folders are skipped. The walk stops after a
    few thousand names so a huge checkout cannot stall the system check.
    """
    seen = 0
    for dirpath, dirnames, filenames in os.walk(project):
        here = Path(dirpath).resolve()
        kept = []
        for name in dirnames:
            if name in _SKIP_WALK or name.startswith("."):
                continue
            child = here / name
            if any((child / marker).is_file() for marker in _NESTED_MARKERS):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            seen += 1
            if seen > 4000:
                return False
            if Path(name).suffix.lower() in suffixes:
                return True
    return False


def _read_package_json(project: Path) -> Optional[Dict[str, Any]]:
    path = project / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _node_package_manager(project: Path) -> str:
    yarn = (project / "yarn.lock").is_file()
    pnpm = (project / "pnpm-lock.yaml").is_file()
    npm = (project / "package-lock.json").is_file()
    if pnpm and not npm:
        return "pnpm"
    if yarn and not npm:
        return "yarn"
    return "npm"


def required_tools(project: Path) -> Dict[str, str]:
    """Tools this project needs, each with a short reason.

    A familiar filename is not enough. ``package.json`` with no scripts and
    no JavaScript, a ``go.mod`` with no Go source, a Dockerfile beside a
    real language project, and Make with no Makefile are not requirements.
    """
    project = Path(project)
    needed: Dict[str, str] = {}
    python_manifest = next((name for name in _PYTHON_MANIFESTS if (project / name).is_file()), "")
    if python_manifest or _has_source(project, {".py"}):
        needed["python"] = f"Python project ({python_manifest})" if python_manifest else "this folder contains Python source"

    package = _read_package_json(project)
    if package is not None:
        scripts = package.get("scripts")
        has_scripts = isinstance(scripts, dict) and any(str(value).strip() for value in scripts.values())
        main = package.get("main")
        has_entry = isinstance(main, str) and (project / main).is_file()
        if has_scripts or has_entry or _has_source(project, _NODE_SOURCE):
            manager = _node_package_manager(project)
            needed["node"] = "this project has JavaScript or TypeScript to run"
            needed[manager] = f"this project installs its packages with {manager}"

    if (project / "go.mod").is_file() and _has_source(project, {".go"}):
        needed["go"] = "this project has Go source"

    if any((project / name).is_file() for name in ("Makefile", "makefile", "GNUmakefile")):
        needed["make"] = "this project has a Makefile"

    if (project / ".git").exists():
        needed["git"] = "this folder is a Git repository"

    docker_files = [
        name for name in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
        if (project / name).is_file()
    ]
    language = [name for name in ("python", "node", "go") if name in needed]
    if docker_files and not language:
        needed["docker"] = "Docker is how this folder is built"
    return needed


def tooling_report(project: Optional[Path] = None) -> Dict[str, Any]:
    """Installed tools, and which missing ones this project actually needs.

    With no project, a tool that is not installed is ``not_needed`` rather
    than an error. Nothing has asked for it yet.
    """
    needed = required_tools(project) if project is not None else {}
    names = list(EXPECTED_TOOLS)
    for tool in needed:
        if tool not in names:
            names.append(tool)
    tools: Dict[str, Dict[str, Any]] = {}
    for tool in names:
        url = EXPECTED_TOOLS.get(tool) or _EXTRA_TOOL_URLS.get(tool)
        found = shutil.which(tool)
        is_needed = tool in needed
        if found:
            status = "ready"
        elif project is not None and is_needed:
            status = "missing"
        else:
            status = "not_needed"
        tools[tool] = {
            "status": status,
            "path": found,
            "url": url if status == "missing" else None,
            "needed": is_needed,
            "reason": needed.get(tool, ""),
        }
    return {"project": str(project) if project is not None else None, "tools": tools}


def check_tooling(project: Optional[Path] = None) -> List[Diagnostic]:
    """Report which of the expected developer tools are installed.

    Pass a project to warn only about tools that project needs. The others
    stay ok, so a Python folder does not fail the check for Docker or Make.
    """
    report = tooling_report(project)
    results: List[Diagnostic] = []
    for tool, info in report["tools"].items():
        found = info["path"]
        if project is not None and not info["needed"]:
            results.append(
                Diagnostic(
                    id=f"tool.{tool}",
                    title=f"{tool} is not required",
                    status=OK,
                    detail="This project does not need it.",
                    data={"path": found, "url": info["url"], "needed": False},
                )
            )
            continue
        url = EXPECTED_TOOLS.get(tool) or _EXTRA_TOOL_URLS.get(tool) or ""
        missing = not found
        # A bare check still lists every absent tool. A project only warns
        # for a tool it needs; the unneeded ones returned above.
        status = OK if found else WARN
        results.append(
            Diagnostic(
                id=f"tool.{tool}",
                title=f"{tool} is installed",
                status=status,
                detail=(found or info["reason"] or f"{tool} was not found on PATH"),
                fix=None if not missing else f"Install {tool}: {url}",
                data={"path": found, "url": url, "needed": info["needed"]},
            )
        )
    return results


def check_tool_versions() -> List[Diagnostic]:
    """Report the version of each tool that is present, and flag oddities."""
    results: List[Diagnostic] = []
    for tool in EXPECTED_TOOLS:
        path = shutil.which(tool)
        if not path:
            continue
        try:
            proc = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            banner = (proc.stdout or proc.stderr).strip().splitlines()
            version = banner[0] if banner else "unknown"
        except Exception as exc:  # noqa: BLE001 - a broken tool is a finding, not a crash
            results.append(
                Diagnostic(
                    id=f"version.{tool}",
                    title=f"{tool} version",
                    status=FAIL,
                    detail=f"Could not run '{tool} --version': {exc}",
                )
            )
            continue

        status = OK
        fix = None
        match = re.search(r"(\d+)\.(\d+)", version)
        if tool == "python" and match and int(match.group(1)) < 3:
            status = FAIL
            fix = "Setup Manager needs Python 3.8 or newer."
        results.append(
            Diagnostic(
                id=f"version.{tool}",
                title=f"{tool} version",
                status=status,
                detail=version,
                fix=fix,
                data={"version": version},
            )
        )
    return results


def check_python_environment() -> List[Diagnostic]:
    """Check that the interpreter running Setup Manager is usable."""
    results: List[Diagnostic] = []
    version = ".".join(str(part) for part in sys.version_info[:3])
    results.append(
        Diagnostic(
            id="python.interpreter",
            title="Interpreter version",
            status=OK if sys.version_info >= (3, 8) else FAIL,
            detail=f"Python {version} ({sys.executable})",
            fix=None if sys.version_info >= (3, 8) else "Upgrade to Python 3.8 or newer.",
        )
    )

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    results.append(
        Diagnostic(
            id="python.venv",
            title="Running inside a virtual environment",
            status=OK if in_venv else WARN,
            detail=sys.prefix,
            fix=None if in_venv else "Create one with: python -m venv venv",
        )
    )
    return results


def check_wsl_config(path: Optional[Path] = None) -> List[Diagnostic]:
    """Validate ``/etc/wsl.conf`` syntax when WSL is present."""
    target = Path(path) if path else Path("/etc/wsl.conf")
    if not target.exists():
        return [
            Diagnostic(
                id="wsl.conf",
                title="WSL configuration",
                status=OK,
                detail="No /etc/wsl.conf on this machine (WSL is probably not used).",
            )
        ]

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return [
            Diagnostic(
                id="wsl.conf",
                title="WSL configuration",
                status=FAIL,
                detail=f"Could not read {target}: {exc}",
            )
        ]

    problems: List[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if "[" in stripped or "]" in stripped:
            continue
        if not _WSL_CONF_LINE.match(line):
            problems.append(f"line {number}: {stripped!r}")

    if problems:
        return [
            Diagnostic(
                id="wsl.conf",
                title="WSL configuration",
                status=FAIL,
                detail="; ".join(problems),
                fix="Every entry in /etc/wsl.conf must be written as 'key = value'.",
                data={"path": str(target), "problems": problems},
            )
        ]
    return [
        Diagnostic(
            id="wsl.conf",
            title="WSL configuration",
            status=OK,
            detail=f"{target} looks valid.",
            data={"path": str(target)},
        )
    ]


def check_zone_identifier(root: Path) -> List[Diagnostic]:
    """Find Windows ``Zone.Identifier`` metadata that leaked into a source tree.

    The usual form is a file called ``something.zip:Zone.Identifier``: a zip
    made on Windows keeps the name, and unpacking it on Linux or WSL leaves
    the metadata behind as a real file that some build tools then choke on.
    """
    offenders: List[str] = []
    try:
        for path in Path(root).rglob("*"):
            if "zone.identifier" in path.name.lower():
                offenders.append(str(path))
                if len(offenders) >= 50:
                    break
    except Exception:  # noqa: BLE001 - unreadable trees are reported elsewhere
        pass

    if offenders:
        return [
            Diagnostic(
                id="zone.identifier",
                title="Windows download metadata in source tree",
                status=WARN,
                detail=f"{len(offenders)} Zone.Identifier file(s) found; they can break builds.",
                fix="Remove the Zone.Identifier files (see the 'strip_zone_identifier' repair).",
                data={"files": offenders},
            )
        ]
    return [
        Diagnostic(
            id="zone.identifier",
            title="Windows download metadata in source tree",
            status=OK,
            detail="No Zone.Identifier files found.",
        )
    ]


def check_broken_symlinks(root: Path) -> List[Diagnostic]:
    """Find symlinks that point nowhere."""
    broken: List[str] = []
    try:
        for path in Path(root).rglob("*"):
            if path.is_symlink() and not path.exists():
                broken.append(str(path))
                if len(broken) >= 50:
                    break
    except Exception:  # noqa: BLE001
        pass

    if broken:
        return [
            Diagnostic(
                id="symlink.broken",
                title="Broken symbolic links",
                status=WARN,
                detail=f"{len(broken)} broken symlink(s) found.",
                fix="Remove or recreate them (see the 'prune_broken_symlinks' repair).",
                data={"files": broken},
            )
        ]
    return [
        Diagnostic(
            id="symlink.broken",
            title="Broken symbolic links",
            status=OK,
            detail="No broken symlinks found.",
        )
    ]


# --------------------------------------------------------------------------- #
# Build checks
# --------------------------------------------------------------------------- #
def diagnose_build_output(output: str) -> List[Diagnostic]:
    """Match captured build output against the knowledge base."""
    results: List[Diagnostic] = []
    if not output or not output.strip():
        return [
            Diagnostic(
                id="build.empty",
                title="Build output",
                status=WARN,
                detail="The build produced no output to diagnose.",
            )
        ]

    for signature, data in KnowledgeBase.iter_matches(output):
        results.append(
            Diagnostic(
                id=f"build.{signature}",
                title=data["name"],
                status=FAIL,
                detail=data["cause"],
                fix=data["fix"],
                data={"signature": signature, **data},
            )
        )

    if not results:
        results.append(
            Diagnostic(
                id="build.unknown",
                title="Build output",
                status=WARN,
                detail="No known failure matched this output.",
                fix="Re-run with more verbosity, or attach the full log when reporting an issue.",
            )
        )
    return results


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #
def run_machine_diagnostics(root: Optional[Path] = None) -> List[Diagnostic]:
    """Run every machine-level check."""
    results: List[Diagnostic] = []
    results.extend(check_tooling(root))
    results.extend(check_tool_versions())
    results.extend(check_python_environment())
    results.extend(check_wsl_config())
    if root is not None:
        results.extend(check_zone_identifier(Path(root)))
        results.extend(check_broken_symlinks(Path(root)))
    return results


def summarize(diagnostics: Iterable[Diagnostic]) -> Dict[str, Any]:
    """Count findings by status."""
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for diagnostic in diagnostics:
        counts[diagnostic.status] = counts.get(diagnostic.status, 0) + 1
    return counts
