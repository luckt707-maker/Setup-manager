"""The launch options the app offers for a project.

A Makefile or a compose file describes several ways to run the project.
The app used to offer one guess ("make", "docker compose up") and left the
user to know which target they actually wanted.
"""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orchestrator import ProjectOrchestrator  # noqa: E402

MAKEFILE = """
build:
\tcc main.c

test:
\tcc -c test.c
"""

COMPOSE = """
services:
  api:
    build: .
  db:
    image: postgres:16
"""


def _project(tmp_path, files):
    project = tmp_path / "proj"
    project.mkdir()
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


def _labels(candidates):
    return [candidate["label"] for candidate in candidates]


@pytest.fixture
def make_installed(monkeypatch):
    """Pretend make is on the PATH, so the make targets can be offered."""
    monkeypatch.setattr(shutil, "which", lambda name: "C:/tools/make.exe" if name == "make" else None)


def test_a_python_program_is_not_reported_as_node_or_docker(tmp_path):
    """Sherlock has a package.json and a Dockerfile. Neither is how it runs."""
    project = _project(tmp_path, {
        "requirements.txt": "requests\n",
        "welcome_launcher.py": "print('hi')\n",
        "package.json": '{"dependencies":{"build":"0.1.4","@dabh/diagnostics":"2.0.9"}}\n',
        "Dockerfile": "FROM python:3.12\n",
    })
    venv_python = project / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
    venv_python.mkdir(parents=True)
    (venv_python / ("python.exe" if sys.platform == "win32" else "python")).write_text("", encoding="utf-8")
    (project / "venv" / "Lib" / "site-packages").mkdir(parents=True)

    report = ProjectOrchestrator(str(project)).diagnose()

    assert report["detected"] == ["python"]
    assert [item["label"] for item in report["candidates"]] == ["Python: welcome_launcher.py"]
    labels = [item["label"] for item in report["install_commands"]]
    assert labels == ["Install Python dependencies"]
    assert "npm" not in report["install_commands"][0]["command"]
    assert str(project / "venv") in report["checks"][1]["detail"]
    assert str(project / "venv") in report["install_commands"][0]["command"]
    assert "-r requirements.txt" in report["install_commands"][0]["command"]


def test_a_loose_python_file_points_at_the_real_start(tmp_path):
    """welcome_launcher.py is a helper. The hint names the real start."""
    project = _project(tmp_path, {
        "welcome_launcher.py": "print('hi')\n",
        "pyproject.toml": "[tool.poetry.scripts]\nsherlock = 'missing_package.sherlock:main'\n",
        "pkg/__main__.py": "print('go')\n",
    })

    report = ProjectOrchestrator(str(project)).analyze()

    hint = report["entry_hint"]
    assert "welcome_launcher.py" in hint
    assert "pkg/__main__.py" in hint
    assert "sherlock command in pyproject.toml" in hint
    assert ".app" not in hint


def test_a_python_file_with_no_other_start_names_the_usual_places(tmp_path):
    project = _project(tmp_path, {"welcome_launcher.py": "print('hi')\n"})

    hint = ProjectOrchestrator(str(project)).analyze()["entry_hint"]

    assert "welcome_launcher.py" in hint
    assert "main.py" in hint
    assert "__main__.py" in hint
    assert ".app" in hint


def test_sherlock_offers_its_command_instead_of_the_welcome_screen(tmp_path):
    """welcome_launcher.py prints a banner. It is not the search command."""
    project = _project(tmp_path, {
        "pyproject.toml": "[tool.poetry.scripts]\nsherlock = 'sherlock_project.sherlock:main'\n",
        "welcome_launcher.py": "print('hi')\n",
        "sherlock_project/__init__.py": "",
        "sherlock_project/__main__.py": "",
    })
    venv_python = project / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
    venv_python.mkdir(parents=True)
    (venv_python / ("python.exe" if sys.platform == "win32" else "python")).write_text("", encoding="utf-8")

    candidates = ProjectOrchestrator(str(project))._get_launch_candidates()

    assert len(candidates) == 1, candidates
    assert candidates[0]["command"][-3:] == ["-m", "sherlock_project", "--help"]
    assert "welcome_launcher.py" not in candidates[0]["command"]


def test_a_built_go_program_runs_its_exe(tmp_path):
    """PhoneInfoga already has a program file. Run that, not go run."""
    project = tmp_path / "PhoneInfoga"
    project.mkdir()
    (project / "go.mod").write_text("module example.com/phoneinfoga\n\ngo 1.22\n", encoding="utf-8")
    (project / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    exe = project / ("phoneinfoga.exe" if sys.platform == "win32" else "phoneinfoga")
    exe.write_bytes(b"")

    candidates = ProjectOrchestrator(str(project))._get_launch_candidates()

    assert [item["command"] for item in candidates] == [[str(exe)]]


def test_a_make_project_offers_a_candidate_for_every_target(tmp_path, make_installed):
    """'make build' and 'make test' are different things to run."""
    project = _project(tmp_path, {"Makefile": MAKEFILE})

    candidates = ProjectOrchestrator(str(project))._get_launch_candidates()

    assert "Make target: build" in _labels(candidates), _labels(candidates)
    assert "Make target: test" in _labels(candidates), _labels(candidates)


def test_the_make_candidate_runs_the_target_it_names(tmp_path, make_installed):
    """A label that does not match the command is worse than no label."""
    project = _project(tmp_path, {"Makefile": MAKEFILE})

    candidates = ProjectOrchestrator(str(project))._get_launch_candidates()

    commands = [candidate["command"] for candidate in candidates]
    assert any(command[-1] == "build" for command in commands), commands


def test_a_compose_project_offers_a_candidate_for_every_service(tmp_path):
    """Each service can be brought up on its own."""
    project = _project(tmp_path, {"docker-compose.yml": COMPOSE})

    candidates = ProjectOrchestrator(str(project))._get_launch_candidates()

    assert "Docker service: api" in _labels(candidates), _labels(candidates)
    assert "Docker service: db" in _labels(candidates), _labels(candidates)


def test_a_plain_dockerfile_offers_a_build_candidate(tmp_path):
    """A Dockerfile with no compose file still has something to run."""
    project = _project(tmp_path, {"Dockerfile": "FROM python:3.12\n"})

    candidates = ProjectOrchestrator(str(project))._get_launch_candidates()

    assert "Docker: build image" in _labels(candidates), _labels(candidates)


def test_a_python_project_is_not_affected(tmp_path):
    """The old options still work."""
    project = _project(tmp_path, {"main.py": "print('hi')\n"})

    candidates = ProjectOrchestrator(str(project))._get_launch_candidates()

    assert "Python: main.py" in _labels(candidates), _labels(candidates)


def test_no_make_target_is_offered_when_make_is_not_installed(tmp_path, monkeypatch):
    """A button that names a program that is not there only fails later."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    project = _project(tmp_path, {"Makefile": MAKEFILE})

    candidates = ProjectOrchestrator(str(project))._get_launch_candidates()

    assert not any(label.startswith("Make target") for label in _labels(candidates)), _labels(candidates)
