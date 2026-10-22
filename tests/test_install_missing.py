"""Installing the packages the scan says are missing.

The scan finds what a project declares but does not have. This is the
other half: putting those packages into the project's own environment.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.setup import Setup  # noqa: E402

POETRY_PROJECT = """
[tool.poetry]
name = "demo"
version = "0.1.0"

[tool.poetry.dependencies]
python = "^3.9"
tomli = "^2.2.1"
requests = ">=2.22.0"
"""


class _RecordingExecutor:
    """Records the commands instead of running them."""

    def __init__(self):
        self.commands = []

    def run(self, command, cwd=None, show_output=False, callback=None):
        self.commands.append(command)
        return True

    def check_installed(self, command):
        return True

    def text(self) -> str:
        return " | ".join(
            command if isinstance(command, str) else " ".join(str(part) for part in command)
            for command in self.commands
        )


def _project(tmp_path, files, packages=()):
    project = tmp_path / "proj"
    project.mkdir()
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    site = project / "venv" / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (project / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
    (project / "venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    for package in packages:
        (site / f"{package}-1.0.dist-info").mkdir()
    return project


def test_the_missing_packages_are_installed(tmp_path):
    """The project asked for tomli; the environment does not have it."""
    project = _project(tmp_path, {"pyproject.toml": POETRY_PROJECT})

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    messages = []
    success = setup.install_missing_packages(callback=messages.append)

    assert success is True, messages
    text = setup.executor.text()
    assert "tomli" in text, text
    assert "requests" in text, text


def test_nothing_is_installed_when_everything_is_present(tmp_path):
    """There is nothing to do, so nothing runs."""
    project = _project(tmp_path, {"pyproject.toml": POETRY_PROJECT}, packages=["tomli", "requests"])

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    messages = []
    success = setup.install_missing_packages(callback=messages.append)

    assert success is True, messages
    assert setup.executor.commands == [], setup.executor.text()


def test_a_project_without_a_virtual_environment_is_told_to_set_up_python(tmp_path):
    """Packages belong to the project's own environment, not to ours."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(POETRY_PROJECT, encoding="utf-8")

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    messages = []
    success = setup.install_missing_packages(callback=messages.append)

    assert success is False, messages
    joined = "\n".join(messages)
    assert "Set up Python" in joined, joined
    assert setup.executor.commands == [], setup.executor.text()
