"""Repairs for the problems the doctor can put right on its own.

Every repair is explicit: the doctor *offers* a repair, and something has to
call :func:`apply_repair` to actually perform it. Nothing here runs
automatically, and every repair reports what it changed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: A repair that rewrites /etc/wsl.conf must keep every setting that was
#: already valid; only the malformed lines are dropped.
_WSL_CONF_LINE = re.compile(r"^\s*(\w+)\s*=\s*(.*)$")


@dataclass
class Repair:
    """A change the doctor knows how to make."""

    id: str
    title: str
    description: str
    applies_to: str
    #: Returns (changed_count, message).
    action: Callable[[], Tuple[int, str]] = field(repr=False, default=None)  # type: ignore[assignment]

    def apply(self) -> Tuple[bool, str]:
        """Run the repair. Returns (changed, message)."""
        if self.action is None:
            return False, "This repair has no action attached."
        try:
            count, message = self.action()
        except Exception as exc:  # noqa: BLE001 - a failed repair is a result, not a crash
            return False, f"Repair failed: {exc}"
        return bool(count), message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "applies_to": self.applies_to,
        }


# --------------------------------------------------------------------------- #
# Individual repairs
# --------------------------------------------------------------------------- #
def strip_zone_identifier(root: Path) -> Repair:
    """Delete the Zone.Identifier alternate data streams Windows adds to
    downloaded files. They are invisible in Explorer but break some build
    tools."""

    def action() -> Tuple[int, str]:
        removed = 0
        for path in Path(root).rglob("*"):
            if "zone.identifier" not in path.name.lower():
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed, f"Removed {removed} Zone.Identifier file(s)."

    return Repair(
        id="strip_zone_identifier",
        title="Remove Windows Zone.Identifier metadata",
        description="Deletes the hidden Zone.Identifier files that Windows attaches to downloaded files.",
        applies_to="zone.identifier",
        action=action,
    )


def prune_broken_symlinks(root: Path) -> Repair:
    """Remove symlinks that point at nothing."""

    def action() -> Tuple[int, str]:
        removed = 0
        for path in Path(root).rglob("*"):
            if path.is_symlink() and not path.exists():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed, f"Removed {removed} broken symlink(s)."

    return Repair(
        id="prune_broken_symlinks",
        title="Remove broken symbolic links",
        description="Deletes symlinks whose target no longer exists.",
        applies_to="symlink.broken",
        action=action,
    )


def fix_wsl_conf(path: Optional[Path] = None) -> Repair:
    """Drop the malformed lines from /etc/wsl.conf, keeping the valid ones."""

    target = Path(path) if path else Path("/etc/wsl.conf")

    def action() -> Tuple[int, str]:
        if not target.exists():
            return 0, f"{target} does not exist."
        text = target.read_text(encoding="utf-8", errors="replace")
        kept: List[str] = []
        dropped: List[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")) or "[" in stripped or "]" in stripped:
                kept.append(line)
                continue
            if _WSL_CONF_LINE.match(line):
                kept.append(line)
            else:
                dropped.append(line)
        if not dropped:
            return 0, f"{target} had no malformed lines."
        backup = target.with_suffix(target.suffix + ".bak")
        backup.write_text(text, encoding="utf-8")
        target.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        return len(dropped), f"Rewrote {target} without {len(dropped)} malformed line(s). Backup: {backup}"

    return Repair(
        id="fix_wsl_conf",
        title="Repair /etc/wsl.conf syntax",
        description="Removes lines that are not written as 'key = value'. A .bak copy is kept.",
        applies_to="wsl.conf",
        action=action,
    )


def clear_pycache(root: Path) -> Repair:
    """Remove __pycache__ directories and stray .pyc files."""

    def action() -> Tuple[int, str]:
        removed = 0
        for path in Path(root).rglob("__pycache__"):
            if not path.is_dir():
                continue
            for child in path.iterdir():
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    continue
            try:
                path.rmdir()
            except OSError:
                pass
        return removed, f"Removed {removed} cached bytecode file(s)."

    return Repair(
        id="clear_pycache",
        title="Clear Python bytecode caches",
        description="Deletes __pycache__ directories and .pyc files, which can hide stale code.",
        applies_to="pycache",
        action=action,
    )


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def available_repairs(diagnostics: List[Any], root: Optional[Path] = None) -> List[Repair]:
    """Return the repairs that match the given findings.

    Only findings that are actually a problem offer a repair; a healthy
    ``/etc/wsl.conf`` does not need fixing.
    """
    found = {d.id for d in diagnostics if getattr(d, "is_problem", False)}
    repairs: List[Repair] = []

    if "zone.identifier" in found and root is not None:
        repairs.append(strip_zone_identifier(Path(root)))
    if "symlink.broken" in found and root is not None:
        repairs.append(prune_broken_symlinks(Path(root)))
    if "wsl.conf" in found:
        repairs.append(fix_wsl_conf())
    if root is not None:
        # Always worth offering: stale bytecode confuses everyone.
        repairs.append(clear_pycache(Path(root)))
    return repairs


def apply_repair(repair: Repair, dry_run: bool = False) -> Tuple[bool, str]:
    """Apply one repair. With dry_run the repair is described but not run."""
    if dry_run:
        return False, f"[dry run] {repair.title}: {repair.description}"
    return repair.apply()
