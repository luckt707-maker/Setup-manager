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
