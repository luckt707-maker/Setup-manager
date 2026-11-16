"""Read a file only when it really lives inside a chosen folder.

A project can contain a link named README.md that points somewhere else.
Resolving the path and checking the result keeps that file out of the
dashboard and out of what we send to a model.
"""
from pathlib import Path
from typing import Optional


def resolve_inside(root: Path, child: Path) -> Optional[Path]:
    """The real file at ``child``, if it is a regular file inside ``root``."""
    root = Path(root).resolve()
    try:
        resolved = Path(child).resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return None
    return resolved


def read_bytes_inside(root: Path, relative: str, max_bytes: int) -> Optional[bytes]:
    """Read a child file, or return None when it escapes or is not text-sized."""
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        return None
    resolved = resolve_inside(root, root / rel)
    if resolved is None:
        return None
    try:
        data = resolved.read_bytes()
    except OSError:
        return None
    if len(data) > max_bytes or b"\0" in data:
        return None
    return data


def read_text_inside(root: Path, relative: str, max_bytes: int) -> Optional[str]:
    """``read_bytes_inside`` decoded as text."""
    data = read_bytes_inside(root, relative, max_bytes)
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")
