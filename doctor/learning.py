"""The doctor remembers fixes that worked.

When a repair is applied *and* the problem it was meant to fix is gone
afterwards, the finding and its fix are added to
``knowledge/known_failures.json``, in the same shape as the entries that
are already there (``signature -> {name, cause, fix}``).

Conservative on purpose: nothing is recorded unless the fix was verified,
an existing signature is never overwritten, and the file is replaced in one
step so a crash cannot leave it half-written.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from .knowledge_base import KNOWLEDGE_DIR, KnowledgeBase

KNOWN_FAILURES_FILE = KNOWLEDGE_DIR / "known_failures.json"


def record_learned_fix(
    signature: str,
    name: str,
    cause: str,
    fix: str,
    path: Optional[Path] = None,
) -> bool:
    """Add one verified fix. Returns True when something new was written."""
    signature = (signature or "").strip()
    if not signature or not fix:
        return False
    # Never save advice that looks like malware: later users would see it.
    if not KnowledgeBase.is_trusted_entry({"name": name, "cause": cause, "fix": fix}):
        return False
    target = Path(path) if path else KNOWN_FAILURES_FILE

    data = {}
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False  # never overwrite a file we could not read
        if not isinstance(data, dict):
            return False

    known = {key.lower() for key in data}
    if path is None:
        known |= {key.lower() for key in KnowledgeBase.KNOWN_FAILURES}
    if signature.lower() in known:
        return False

    data[signature] = {"name": name or signature, "cause": cause or "", "fix": fix}

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".known_failures-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True
