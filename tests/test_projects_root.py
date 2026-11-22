"""The projects folder is the same for the shortcut, run.bat and the dashboard."""

from pathlib import Path

from fastapi.testclient import TestClient

import app as webapp
from core.settings import (
    allowed_project_roots,
    projects_root,
    projects_root_notes,
    root_problem,
    settings_manager,
)

H = {"X-Setup-Token": webapp.API_TOKEN}


def _client():
    return TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def _use_home(tmp_path, monkeypatch):
    """A pretend user folder, so the test does not touch the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("core.settings.Path.home", lambda: home)
    settings_manager.set("paths", "safe_root", "")
    return home


def test_the_default_folder_is_created_in_the_user_profile(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    monkeypatch.delenv("SETUP_MANAGER_SAFE_ROOT", raising=False)

    folder = projects_root()

    assert folder == (home / "Projects").resolve()
    assert folder.is_dir()
    assert any("Created" in note for note in projects_root_notes())


def test_the_home_folder_itself_falls_back_to_projects(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    monkeypatch.setenv("SETUP_MANAGER_SAFE_ROOT", str(home))

    folder = projects_root()

    assert folder == (home / "Projects").resolve()
    assert folder.is_dir()
    text = " ".join(projects_root_notes())
    assert "home folder" in text
    assert "run.local.bat" in text


def test_downloads_and_desktop_are_refused(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    for name in ("Downloads", "Desktop"):
        unpack = home / name
        unpack.mkdir()
        problem = root_problem(str(unpack))
        assert problem
        assert "unpacked" in problem


def test_a_real_folder_still_wins(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    chosen = tmp_path / "work"
    chosen.mkdir()
    monkeypatch.setenv("SETUP_MANAGER_SAFE_ROOT", str(chosen))

    assert projects_root() == chosen.resolve()
    assert projects_root_notes() == []
    assert home / "Projects" not in allowed_project_roots()


def test_an_unsafe_environment_folder_is_not_opened(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    (home / "Downloads").mkdir()
    monkeypatch.setenv("SETUP_MANAGER_SAFE_ROOT", str(home / "Downloads"))

    roots = allowed_project_roots()

    assert roots == [(home / "Projects").resolve()]


def test_the_dashboard_refuses_downloads_and_still_lists_projects(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    downloads = home / "Downloads"
    downloads.mkdir()
    monkeypatch.delenv("SETUP_MANAGER_SAFE_ROOT", raising=False)

    refused = _client().post("/settings/safe-root", json={"path": str(downloads)}, headers=H)
    assert refused.status_code == 400
    assert "unpacked" in refused.json()["detail"]

    listed = _client().get("/projects", headers=H)
    assert listed.status_code == 200
    assert (home / "Projects").is_dir()


def test_run_bat_does_not_use_the_download_folder_as_the_projects_folder():
    text = Path("run.bat").read_text(encoding="utf-8")
    assert "SETUP_MANAGER_SAFE_ROOT=%~dp0" not in text.replace(" ", "")
    assert "python.org/downloads" in text
    assert "Set as projects root" in text
