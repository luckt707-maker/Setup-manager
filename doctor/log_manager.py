"""Keeps a history of doctor runs.

Each run is appended as one JSON object per line to ``logs/doctor-YYYY-MM.jsonl``
so the history is easy to read with a text editor and easy to parse.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class DoctorLogManager:
    """Append-only history of doctor runs."""

    def __init__(self, log_dir: Optional[Path] = None):
        self.log_dir = Path(log_dir) if log_dir else LOG_DIR

    def _file_for(self, when: datetime) -> Path:
        return self.log_dir / f"doctor-{when.strftime('%Y-%m')}.jsonl"

    def record(self, entry: Dict[str, Any], when: Optional[datetime] = None) -> Path:
        """Append one run to the history. Returns the file that was written."""
        stamp = when or datetime.now(timezone.utc)
        payload = {"timestamp": stamp.isoformat(), **entry}
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            target = self._file_for(stamp)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")
            return target
        except OSError:
            # A read-only install should not break a doctor run.
            return self.log_dir

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the most recent runs, newest first."""
        if not self.log_dir.is_dir():
            return []
        entries: List[Dict[str, Any]] = []
        files = sorted(self.log_dir.glob("doctor-*.jsonl"), reverse=True)
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
                if len(entries) >= limit:
                    return entries
        return entries

    def clear(self) -> int:
        """Delete the history. Returns how many entries were removed."""
        removed = 0
        if not self.log_dir.is_dir():
            return 0
        for path in self.log_dir.glob("doctor-*.jsonl"):
            try:
                removed += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
                path.unlink()
            except OSError:
                continue
        return removed
