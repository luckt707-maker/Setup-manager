"""Tests for the remaining hardening work: preflight scans, stdin, state file."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as webapp  # noqa: E402
from app import app  # noqa: E402
from core import executor as executor_module  # noqa: E402
from core.executor import CommandExecutor  # noqa: E402


def _client():
    return TestClient(app, base_url="http://127.0.0.1", headers={"x-setup-token": webapp.API_TOKEN})


# --------------------------------------------------------------------------- #
#  A child process must never wait for the user's keyboard
# --------------------------------------------------------------------------- #

def test_a_child_does_not_inherit_the_console(monkeypatch):
    """A child that reads stdin must not block the task for 10 minutes.

    The dashboard runs commands in the background, where nobody can answer
    a prompt, so the child gets an empty stdin instead of the console.
    """
    captured = {}
    real_popen = subprocess.Popen

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(executor_module.subprocess, "Popen", fake_popen)
    CommandExecutor.run([sys.executable, "-c", "pass"])

    assert captured.get("stdin") == subprocess.DEVNULL


# --------------------------------------------------------------------------- #
#  The endpoints that run project code are scanned first
# --------------------------------------------------------------------------- #

class _StubSetup:
    """Stands in for the real Setup so the test does not change the machine."""

    def __init__(self, path):
        self.path = path

    def setup_python(self, callback=None):
        return True

    def setup_nodejs(self, callback=None):
        return True

    def setup_dotnet(self, callback=None):
        return True

    def setup_ruby(self, callback=None):
        return True

    def setup_git(self, callback=None):
        return True

    def setup_make(self, callback=None):
        return True

    def install_extensions(self, project_type, ext_types=None, callback=None):
        return True

    def run_quality_checks(self, project_type, callback=None):
        return {}


def _project(tmp_path) -> Path:
    """A project folder inside the safe root the tests run against."""
    safe_root = Path(os.environ["SETUP_MANAGER_SAFE_ROOT"])
    project = safe_root / "proj"
    project.mkdir(parents=True, exist_ok=True)
    return project


@pytest.mark.parametrize(
    "url,params,json_body",
    [
        ("/task/setup/python", {"path": "PROJECT"}, None),
        ("/task/extensions", {"path": "PROJECT"}, {"project_type": "python"}),
        ("/task/checks", {"path": "PROJECT", "project_type": "auto"}, None),
    ],
)
def test_endpoints_that_run_project_code_scan_it_first(monkeypatch, tmp_path, url, params, json_body):
    """npm install, pip install and pytest all run code from the project."""
    calls = []

    def fake_scan(path):
        calls.append(Path(path))
        return {"status": "clean", "detections": 0}

    monkeypatch.setattr(webapp, "_scan_with_defender", fake_scan)
    # The real setup would create a virtual environment and install
    # packages; only the scan is what this test is about.
    monkeypatch.setattr(webapp, "Setup", _StubSetup)

    project = str(_project(tmp_path))
    params = {key: (project if value == "PROJECT" else value) for key, value in params.items()}

    client = _client()
    response = client.post(url, params=params, json=json_body)

    assert response.status_code == 200, response.text
    assert calls, f"{url} did not scan the project before running its code"


def test_a_threat_blocks_the_task_before_anything_runs(monkeypatch, tmp_path):
    """A scan that finds something must stop the task, not warn about it."""
    def fake_scan(path):
        return {"status": "threats", "detections": 1}

    monkeypatch.setattr(webapp, "_scan_with_defender", fake_scan)

    response = _client().post("/task/setup/python", params={"path": str(_project(tmp_path))})

    assert response.status_code == 400, response.text


# --------------------------------------------------------------------------- #
#  The state file belongs to the instance that wrote it
# --------------------------------------------------------------------------- #

def test_a_state_file_from_another_instance_is_left_alone(tmp_path):
    """Two servers must not delete each other's state file."""
    state_path = tmp_path / "server.json"
    state_path.write_text('{"port": 9999, "pid": 12345}', encoding="utf-8")

    webapp.clear_state(state_path)

    assert state_path.exists(), "another instance's state file was deleted"


def test_our_own_state_file_is_removed(tmp_path):
    state_path = tmp_path / "server.json"
    state_path.write_text('{"port": 8010, "pid": %d}' % __import__("os").getpid(), encoding="utf-8")

    webapp.clear_state(state_path)

    assert not state_path.exists()


# --------------------------------------------------------------------------- #
#  A file the user picks still has to come from somewhere sensible
# --------------------------------------------------------------------------- #

def test_mounting_an_image_from_outside_the_allowed_roots_is_blocked():
    """Mounting a disk image is a privileged act on any path it is given."""
    response = _client().post("/mount", json={"image_path": r"C:\Windows\Temp\evil.iso"})

    assert response.status_code == 400, response.text
    assert "outside" in response.json().get("detail", "").lower()


def test_scanning_a_file_from_outside_the_allowed_roots_is_blocked():
    """A scan of any path on the machine is a way to burn the CPU."""
    response = _client().post("/file-safety-scan", json={"path": r"C:\Windows\Temp\evil.exe"})

    assert response.status_code == 400, response.text
    assert "outside" in response.json().get("detail", "").lower()


def test_a_file_inside_the_projects_root_is_allowed(tmp_path):
    """The normal case: a file the user keeps with their projects."""
    target = _project(tmp_path) / "download.zip"
    target.write_bytes(b"not really a zip")

    response = _client().post("/file-safety-scan", json={"path": str(target)})

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
#  Installing a project's dependencies must not run the project's code
# --------------------------------------------------------------------------- #

class _RecordingExecutor:
    """Records the commands instead of running them."""

    def __init__(self):
        self.commands = []

    def run(self, command, cwd=None, show_output=False, callback=None):
        self.commands.append(command)
        return True

    def run_with_code(self, command, cwd=None, show_output=False, callback=None, skip_exit_codes=None):
        self.commands.append(command)
        return True, 0

    def check_installed(self, command):
        return True

    def text(self) -> str:
        """Every recorded command as one readable string."""
        parts = []
        for command in self.commands:
            if isinstance(command, str):
                parts.append(command)
            else:
                parts.append(" ".join(str(part) for part in command))
        return " | ".join(parts)


def test_requirements_are_installed_without_building_them(tmp_path):
    """A source install runs the project's setup.py."""
    from core.setup import Setup

    project = tmp_path / "proj"
    project.mkdir()
    (project / "requirements.txt").write_text("some-package==1.0\n", encoding="utf-8")

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    setup.setup_python()

    assert "--only-binary=:all:" in setup.executor.text(), setup.executor.text()


def test_npm_install_does_not_run_the_projects_scripts(tmp_path):
    """npm lifecycle scripts run whatever the package asks for."""
    from core.setup import Setup

    project = tmp_path / "proj"
    project.mkdir()
    (project / "package.json").write_text('{"name": "x"}', encoding="utf-8")

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    setup.setup_nodejs()

    assert "--ignore-scripts" in setup.executor.text(), setup.executor.text()
