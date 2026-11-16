"""Find a program when the name is known and the folder is not.

Setup Manager can look in more than one projects folder. This search checks
the folders directly inside those roots and says, in one sentence, whether
the match is the source, only a Python environment, or this dashboard.
"""

from __future__ import annotations

import os
from pathlib import Path

_SKIP = {
    "node_modules", "venv", ".venv", ".git", "__pycache__", "site-packages",
    "dist", "build",
}
_MARKERS = (
    "go.mod", "pyproject.toml", "requirements.txt", "setup.py", "Pipfile",
    "package.json", "Cargo.toml", "Makefile", "makefile", "pom.xml",
    "build.gradle", "composer.json", "Gemfile", "Package.swift",
)
_ENV_NAMES = {"venv", ".venv"}
_BORING_EXE = {
    "python.exe", "pythonw.exe", "pip.exe", "pip3.exe", "pip3.13.exe",
    "pip3.14.exe", "activate.bat",
}
# Console scripts that ride along with libraries. They are not the program.
_NOISE_TOOLS = {
    "f2py", "idna", "normalizer", "numpy-config", "py.test", "pytest",
    "httpx", "uvicorn", "fastapi", "dotenv", "pygmentize", "tqdm",
}


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _env_tools(folder: Path) -> list[str]:
    """Program names installed inside a venv, ignoring Python itself."""
    found = []
    for env_name in ("venv", ".venv"):
        scripts = folder / env_name / ("Scripts" if os.name == "nt" else "bin")
        if not scripts.is_dir():
            continue
        try:
            names = sorted(p.name for p in scripts.iterdir() if p.suffix.lower() == ".exe" or (os.name != "nt" and p.is_file()))
        except OSError:
            continue
        for name in names:
            if name.lower() in _BORING_EXE or name.lower().startswith("pip"):
                continue
            if os.name != "nt" and name.startswith("python"):
                continue
            label = Path(name).stem
            if label.lower() in _NOISE_TOOLS:
                continue
            if label not in found:
                found.append(label)
    wanted = folder.name.lower()
    named = [label for label in found if label.lower() == wanted]
    if named:
        return named
    return found[:3]


def _is_dashboard(folder: Path) -> bool:
    return folder.name.lower() in {"setupmanagerv2", "setup-manager"} and (folder / "app.py").is_file()


def describe_folder(folder: Path) -> str:
    """One sentence a first visit can act on."""
    try:
        names = {item.name for item in folder.iterdir()}
    except OSError:
        return "I could not read this folder."

    if _is_dashboard(folder):
        return "This is Setup Manager, the page you are using. It is not a program to set up."

    has_py = any(item.suffix.lower() == ".py" and item.name.lower() != "setup.py" for item in folder.glob("*.py"))
    has_source = any((folder / marker).is_file() for marker in _MARKERS) or (folder / "main.go").is_file() or has_py
    env_only = not has_source and names and names <= _ENV_NAMES | {name for name in names if name.startswith(".")}
    # A folder whose visible entries are only venv and .venv.
    visible = {name for name in names if not name.startswith(".")}
    if not has_source and visible and visible <= {"venv"}:
        env_only = True

    if env_only:
        tools = _env_tools(folder)
        if tools:
            listed = ", ".join(tools)
            return (
                f"This folder only has a Python environment. {listed} is already installed there. "
                "The source code is a different folder."
            )
        return "This folder only has a Python environment. The source code is a different folder."

    built = any(folder.glob("*.exe")) or (folder / "bin").is_dir()
    if (folder / "go.mod").is_file() or (folder / "main.go").is_file():
        extra = " A program file is already in this folder." if built else ""
        return "This looks like the program itself (Go). Open this folder to set it up." + extra
    if (folder / "pyproject.toml").is_file() or (folder / "requirements.txt").is_file() or (folder / "setup.py").is_file():
        return "This looks like the program itself (Python). Open this folder to set it up."
    if has_py:
        return "This is a small Python folder. Open it if this is the one you meant."
    if has_source:
        return "This folder has project files. Open it if the name is the one you want."
    return "I found this folder by name. I do not see a program to set up inside it."


def find_programs(roots: list[Path], query: str, limit: int = 20) -> list[dict]:
    """Folders directly inside ``roots`` whose names match ``query``."""
    needle = _norm(query)
    if len(needle) < 2:
        return []
    matches = []
    seen = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        for child in children:
            if len(matches) >= limit:
                return matches
            if not child.is_dir() or child.name.startswith(".") or child.name.lower() in _SKIP:
                continue
            if child.is_symlink():
                continue
            if needle not in _norm(child.name):
                continue
            try:
                key = str(child.resolve())
            except OSError:
                key = str(child)
            if key in seen:
                continue
            seen.add(key)
            matches.append({
                "name": child.name,
                "path": str(child),
                "note": describe_folder(child),
            })
    return matches
