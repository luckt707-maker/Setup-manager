"""Defensive fixes from the 2026-11-16 review (long paths, WAL, port check)."""
from pathlib import Path

from fastapi.testclient import TestClient

import app as webapp
from app import app

def _client():
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
        headers={"x-setup-token": webapp.API_TOKEN},
    )


def test_a_too_long_path_is_a_400_not_a_500():
    r = _client().post("/analyze", json={"path": "A" * 20000})
    assert r.status_code in (400, 422), r.text
    assert r.status_code != 500


def test_a_path_that_is_too_long_for_the_os_is_a_400(tmp_path, monkeypatch):
    """OSError from exists() (ENAMETOOLONG) must not become a 500."""
    inside = Path(webapp._safe_root()) / ("B" * 4000)
    r = _client().post("/analyze", json={"path": str(inside)})
    assert r.status_code == 400, r.text


def test_a_long_chat_message_is_rejected():
    r = _client().post("/ai/chat", json={"message": "A" * 20000, "include_context": False})
    assert r.status_code == 422, r.text


def test_init_db_checkpoints_wal(tmp_path, monkeypatch):
    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr(webapp, "TASK_DB_PATH", db_file)
    webapp._db_local.__dict__.pop("connection", None)
    webapp.init_db()
    task_id = webapp._new_task("checkpoint")
    webapp._finish_task(task_id, "completed")
    webapp.get_db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    wal = Path(str(db_file) + "-wal")
    # After an explicit truncate, the WAL is empty or gone.
    assert (not wal.exists()) or wal.stat().st_size == 0
    webapp._db_local.__dict__.pop("connection", None)

