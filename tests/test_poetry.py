"""A project that declares its dependencies the Poetry way.

The Setup Manager only knew about requirements.txt, so a Poetry project
got an empty virtual environment and every check then failed on a missing
module.
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
colorama = "^0.4.1"

[tool.poetry.group.dev.dependencies]
pytest = "^8.4.2"
"""


class _RecordingExecutor:
    """Records the commands instead of running them."""

    def __init__(self, exit_code=0):
        self.commands = []
        self.exit_code = exit_code

    def run(self, command, cwd=None, show_output=False, callback=None):
        self.commands.append(command)
        return True

    def run_with_code(self, command, cwd=None, show_output=False, callback=None, skip_exit_codes=None):
        self.commands.append(command)
        return self.exit_code == 0, self.exit_code

    def check_installed(self, command):
        return True

    def text(self) -> str:
        parts = []
        for command in self.commands:
            if isinstance(command, str):
                parts.append(command)
            else:
                parts.append(" ".join(str(part) for part in command))
        return " | ".join(parts)


def _project(tmp_path, files):
    project = tmp_path / "proj"
    project.mkdir()
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


def test_a_poetry_project_gets_its_dependencies(tmp_path):
    """Without this the project's own modules cannot even be imported."""
    project = _project(tmp_path, {"pyproject.toml": POETRY_PROJECT})

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    setup.setup_python()

    text = setup.executor.text()
    assert "tomli" in text, text
    assert "requests" in text, text
    assert "pytest" in text, text


def test_a_poetry_caret_is_turned_into_a_pip_range(tmp_path):
    """'^2.2.1' means '>=2.2.1,<3.0.0' to pip."""
    project = _project(tmp_path, {"pyproject.toml": POETRY_PROJECT})

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    setup.setup_python()

    text = setup.executor.text()
    assert ">=2.2.1,<3.0.0" in text, text
    assert ">=0.4.1,<0.5.0" in text, text


def test_the_python_constraint_is_not_installed_as_a_package(tmp_path):
    """'python = "^3.9"' is a version constraint, not a package."""
    project = _project(tmp_path, {"pyproject.toml": POETRY_PROJECT})

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    setup.setup_python()

    text = setup.executor.text()
    assert "pip install python" not in text, text
    assert ">=3.9,<4.0.0" not in text, text


def test_a_requirements_project_still_works(tmp_path):
    """The old path must not change."""
    project = _project(tmp_path, {"requirements.txt": "requests\n"})

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    setup.setup_python()

    text = setup.executor.text()
    assert "requirements.txt" in text, text
    assert "--only-binary=:all:" in text, text


def test_a_pytest_project_is_not_also_run_through_unittest(tmp_path):
    """Two test runners give two verdicts about the same tests.

    unittest discover imports every package in the tree, so a package
    that cannot be imported is reported as a failing test.
    """
    project = _project(
        tmp_path,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "pyproject.toml": POETRY_PROJECT,
            "tests/test_demo.py": "def test_ok():\n    assert True\n",
        },
    )

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    setup._check_available = lambda command: True

    results = setup.run_quality_checks("python")

    assert "unittest" not in results, results
    assert "pytest" in results, results


def test_a_project_without_pytest_still_uses_unittest(tmp_path):
    """The fallback stays for projects that do not use pytest."""
    project = _project(tmp_path, {"tests/test_demo.py": "def test_ok():\n    assert True\n"})

    setup = Setup(str(project))
    setup.executor = _RecordingExecutor()
    setup._check_available = lambda command: True

    results = setup.run_quality_checks("python")

    assert "unittest" in results, results


def test_a_missing_dependency_is_explained(tmp_path):
    """'No module named tomli' means the dependencies are not installed."""
    project = _project(tmp_path, {"pyproject.toml": POETRY_PROJECT})
    messages = []

    class _FailingExecutor(_RecordingExecutor):
        def run_with_code(self, command, cwd=None, show_output=False, callback=None, skip_exit_codes=None):
            self.commands.append(command)
            return False, 1

    setup = Setup(str(project))
    setup.executor = _FailingExecutor()
    setup._check_available = lambda command: True

    setup.run_quality_checks("python", callback=messages.append)

    joined = "\n".join(messages)
    assert "dependencies are not installed" in joined, joined


def test_a_poetry_project_knows_what_to_install(tmp_path):
    """The install button has to work for a project that has no requirements.txt."""
    project = _project(tmp_path, {"pyproject.toml": POETRY_PROJECT})

    setup = Setup(str(project))
    packages = setup.python_install_requirements()

    assert "tomli>=2.2.1,<3.0.0" in packages, packages
    assert "requests>=2.22.0" in packages, packages


def test_a_requirements_project_is_installed_from_its_file(tmp_path):
    """The old path stays the same."""
    project = _project(tmp_path, {"requirements.txt": "requests\n"})

    setup = Setup(str(project))

    assert setup.python_install_requirements() == ["-r", "requirements.txt"]
