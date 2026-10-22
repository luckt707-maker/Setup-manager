"""Reading the ways a project can be run: Makefile targets and compose services.

A Makefile usually has a dozen targets and a compose file a few services.
Offering "make" on its own leaves the user to know which target they want,
so the app reads both files and offers the real choices.
"""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tooling import compose_services, make_command, make_targets  # noqa: E402

MAKEFILE = """
.PHONY: all build test clean

all: build test

build:
\tpython -m build

test:
\tpython -m pytest

clean:
\trm -rf build
"""

COMPOSE = """
version: "3.9"

services:
  api:
    build: .
    ports:
      - "8000:8000"

  db:
    image: postgres:16
    volumes:
      - data:/var/lib/postgresql/data

volumes:
  data:
"""


def _project(tmp_path, files):
    project = tmp_path / "proj"
    project.mkdir()
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


def test_the_makefile_targets_are_read(tmp_path):
    """The targets are what the user can actually run."""
    project = _project(tmp_path, {"Makefile": MAKEFILE})

    targets = make_targets(project)

    assert "build" in targets, targets
    assert "test" in targets, targets
    assert "clean" in targets, targets


def test_the_first_target_is_the_default_one(tmp_path):
    """Make runs the first target when it is not told what to do."""
    project = _project(tmp_path, {"Makefile": MAKEFILE})

    targets = make_targets(project)

    assert targets[0] == "all", targets


def test_an_internal_target_is_not_offered(tmp_path):
    """.PHONY is bookkeeping, not something to run."""
    project = _project(tmp_path, {"Makefile": MAKEFILE})

    targets = make_targets(project)

    assert not any(target.startswith(".") for target in targets), targets


def test_a_pattern_rule_is_not_offered(tmp_path):
    """'%.o: %.c' is a rule, not a target the user would pick."""
    project = _project(tmp_path, {"Makefile": "build:\n\tcc main.c\n\n%.o: %.c\n\tcc -c $<\n"})

    targets = make_targets(project)

    assert targets == ["build"], targets


def test_a_makefile_that_is_not_there_gives_no_targets(tmp_path):
    """Nothing to read, nothing to offer."""
    project = _project(tmp_path, {"main.py": "print('hi')\n"})

    assert make_targets(project) == []


def test_a_lowercase_makefile_is_found(tmp_path):
    """'makefile' and 'GNUmakefile' are the same thing to make."""
    project = _project(tmp_path, {"makefile": "build:\n\tcc main.c\n"})

    assert make_targets(project) == ["build"]


def test_the_compose_services_are_read(tmp_path):
    """Each service is something the user can bring up."""
    project = _project(tmp_path, {"docker-compose.yml": COMPOSE})

    services = compose_services(project)

    assert services == ["api", "db"], services


def test_a_compose_file_that_is_not_there_gives_no_services(tmp_path):
    """Nothing to read, nothing to offer."""
    project = _project(tmp_path, {"Dockerfile": "FROM python:3.12\n"})

    assert compose_services(project) == []


def test_the_modern_compose_file_name_is_found(tmp_path):
    """compose.yaml replaced docker-compose.yaml."""
    project = _project(tmp_path, {"compose.yaml": COMPOSE})

    assert compose_services(project) == ["api", "db"]


def test_the_make_command_is_the_one_that_is_installed(monkeypatch):
    """Make is called differently on different machines."""
    monkeypatch.setattr(shutil, "which", lambda name: "C:/tools/make.exe" if name == "make" else None)

    assert make_command() == "make"


def test_the_windows_make_is_found_when_there_is_no_make(monkeypatch):
    """mingw32-make is what MSYS2 installs."""
    monkeypatch.setattr(shutil, "which", lambda name: "C:/msys/mingw32-make.exe" if name == "mingw32-make" else None)

    assert make_command() == "mingw32-make"


def test_no_make_installed_means_no_make_command(monkeypatch):
    """Then there is nothing to run the Makefile with."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert make_command() is None
