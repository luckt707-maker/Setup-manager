"""Shared test setup for Setup Manager v2.

Puts the project root on ``sys.path`` and points the app at a throwaway
safe root and task database so nothing touches the real machine state.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: A temporary directory that stands in for C:\\dev during the tests.
SAFE_ROOT = Path(tempfile.mkdtemp(prefix="setup-manager-tests-"))
os.environ["SETUP_MANAGER_SAFE_ROOT"] = str(SAFE_ROOT)
os.environ["SETUP_MANAGER_TASKS_DB"] = str(SAFE_ROOT / "tasks-test.db")
