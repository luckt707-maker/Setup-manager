"""Hardening tests for the findings the review crew raised.

Each test names the behaviour that was broken, so a regression is obvious.
"""
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as webapp  # noqa: E402
from app import app  # noqa: E402
from core.cmdline import split_command  # noqa: E402
from core.setup import Setup  # noqa: E402


def _client(token: bool = True):
    # base_url must be an allowed host: TrustedHostMiddleware rejects "testserver".
    headers = {"x-setup-token": webapp.API_TOKEN} if token else {}
    return TestClient(app, base_url="http://127.0.0.1", headers=headers)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the task database at a temporary file."""
    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr(webapp, "TASK_DB_PATH", db_file)
    webapp._db_local.__dict__.pop("connection", None)
    webapp.init_db()
    yield db_file
    webapp._db_local.__dict__.pop("connection", None)


# --------------------------------------------------------------------------- #
#  The origin check must match the host exactly
# --------------------------------------------------------------------------- #

def test_an_origin_that_merely_looks_like_localhost_is_not_trusted():
    """"http://localhost.evil.com" is a domain anyone can register."""
    r = _client().get("/api/config", headers={"Origin": "http://localhost.evil.com"})
    assert r.status_code == 403, r.text


def test_a_loopback_origin_from_another_port_is_not_trusted():
    """A local dev server on another port is not the dashboard."""
    r = _client().get("/api/config", headers={"Origin": "http://127.0.0.1:3000"})
    assert r.status_code == 403, r.text


def test_the_dashboards_own_origin_is_trusted():
    r = _client().get("/api/config", headers={"Origin": "http://127.0.0.1"})
    assert r.status_code == 200, r.text


def test_a_vscode_webview_is_still_trusted():
    """The extensions that embed the dashboard cannot be given a token."""
    r = _client(token=False).get("/api/config", headers={"Origin": "vscode-webview://abc"})
    assert r.status_code == 200, r.text


def test_the_token_is_always_required_for_ordinary_requests():
    """No origin may waive the token."""
    for origin in ("http://localhost:3000", "http://127.0.0.1", "http://evil.com"):
        r = _client(token=False).get("/api/config", headers={"Origin": origin})
        assert r.status_code in (401, 403), (origin, r.status_code)


# --------------------------------------------------------------------------- #
#  A garbled token is a 401, not a crash
# --------------------------------------------------------------------------- #

def test_a_token_with_odd_characters_is_rejected_not_crashed():
    """Header bytes are decoded as latin-1, so a token can be non-ASCII."""
    assert webapp._token_ok("café") is False
    assert webapp._token_ok("") is False
    assert webapp._token_ok(None) is False


def test_the_real_token_is_accepted():
    assert webapp._token_ok(webapp.API_TOKEN) is True


# --------------------------------------------------------------------------- #
#  Log lines get unique sequence numbers
# --------------------------------------------------------------------------- #

def test_concurrent_log_writes_get_unique_sequence_numbers(temp_db):
    """A cancel and the command's output are written from two threads."""
    task_id = webapp._new_task("race")
    errors = []

    def worker(index: int) -> None:
        try:
            for line in range(40):
                webapp._append_log(task_id, f"thread {index} line {line}")
        except Exception as exc:  # noqa: BLE001 - the failure is the point
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    rows = webapp.get_db().execute(
        "SELECT seq FROM task_logs WHERE task_id = ?", (task_id,)
    ).fetchall()
    seqs = [row["seq"] for row in rows]
    assert len(seqs) == 160
    assert len(set(seqs)) == 160


# --------------------------------------------------------------------------- #
#  A task reports what actually happened
# --------------------------------------------------------------------------- #

def test_failed_checks_make_the_task_fail():
    assert webapp._checks_status({"python: pytest": "fail"}) == "failed"
    assert webapp._checks_status({"python: pytest": "pass"}) == "completed"
    assert webapp._checks_status({"python: pytest": "skip"}) == "completed"
    assert webapp._checks_status({}) == "completed"


def test_a_cancelled_task_is_not_reported_as_completed(temp_db):
    task_id = webapp._new_task("cancel me")
    with webapp._cancel_lock:
        webapp.CANCEL_EVENTS[task_id] = threading.Event()
    webapp.CANCEL_EVENTS[task_id].set()

    webapp._finish_task(task_id, "completed")

    row = webapp.get_db().execute(
        "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    assert row["status"] == "cancelled"


# --------------------------------------------------------------------------- #
#  Command lines keep their Windows paths
# --------------------------------------------------------------------------- #

def test_a_windows_path_survives_splitting():
    assert split_command(r"python C:\dev\proj\main.py") == [
        "python",
        r"C:\dev\proj\main.py",
    ]


def test_a_relative_path_survives_splitting():
    assert split_command(r".\scripts\run.py") == [r".\scripts\run.py"]


def test_a_quoted_path_with_spaces_survives_splitting():
    assert split_command(r'"C:\Program Files\node\node.exe" --version') == [
        r"C:\Program Files\node\node.exe",
        "--version",
    ]


def test_plain_words_are_split_on_whitespace():
    assert split_command("npm install --save-dev eslint") == [
        "npm",
        "install",
        "--save-dev",
        "eslint",
    ]


# --------------------------------------------------------------------------- #
#  The project's own interpreter, never the Setup Manager's
# --------------------------------------------------------------------------- #

def test_extensions_are_not_installed_into_the_managers_own_python(tmp_path):
    """A project with no virtual environment gets nothing, not our venv."""
    project = tmp_path / "proj"
    project.mkdir()
    recorded = []

    class RecordingExecutor:
        def run(self, command, cwd=None, show_output=False, callback=None):
            recorded.append(command)
            return True

    setup = Setup(str(project))
    setup.executor = RecordingExecutor()
    setup.install_extensions("python")

    for command in recorded:
        assert "pip" not in " ".join(command) or str(tmp_path) in " ".join(command), command


def test_a_dot_venv_project_uses_its_own_interpreter(tmp_path):
    """uv and modern tools use .venv, not venv."""
    project = tmp_path / "proj"
    python = project / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    recorded = []

    class RecordingExecutor:
        def run(self, command, cwd=None, show_output=False, callback=None):
            recorded.append(command)
            return True

    setup = Setup(str(project))
    setup.executor = RecordingExecutor()
    setup.install_extensions("python")

    pip_commands = [c for c in recorded if "pip" in " ".join(c)]
    assert pip_commands, recorded
    for command in pip_commands:
        assert str(python) in " ".join(command), command


def test_commands_that_are_not_installations_are_skipped(tmp_path):
    """'python -m cProfile' is a run command, not something to install."""
    project = tmp_path / "proj"
    project.mkdir()
    recorded = []

    class RecordingExecutor:
        def run(self, command, cwd=None, show_output=False, callback=None):
            recorded.append(command)
            return True

    setup = Setup(str(project))
    setup.executor = RecordingExecutor()
    setup.install_extensions("python")

    joined = " ".join(" ".join(command) for command in recorded)
    assert "cProfile" not in joined
    assert "pdb" not in joined
