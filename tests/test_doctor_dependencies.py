"""The doctor reporting what a project is missing.

The doctor already knows about the machine. These are the checks that look
inside a project folder: the packages it declares, the modules it imports
and the files it points at.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import tooling  # noqa: E402
from doctor.dependencies import check_project_dependencies  # noqa: E402


def _project(tmp_path, files):
    project = tmp_path / "proj"
    project.mkdir()
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


def _venv(project, packages=()):
    """A virtual environment with the given packages in it."""
    layout = "Lib/site-packages" if sys.platform == "win32" else "lib/python3.12/site-packages"
    site = project / "venv" / layout
    site.mkdir(parents=True, exist_ok=True)
    for package in packages:
        (site / f"{package}-1.0.dist-info").mkdir()
    return site


def _by_id(findings):
    return {finding.id: finding for finding in findings}


def test_a_declared_package_that_is_not_installed_is_reported(tmp_path):
    """The project says it needs requests; the environment does not have it."""
    project = _project(tmp_path, {"requirements.txt": "requests>=2.22.0\n"})
    _venv(project)

    findings = _by_id(check_project_dependencies(project))

    assert "dependencies.not_installed" in findings, findings
    assert "requests" in findings["dependencies.not_installed"].detail


def test_an_installed_package_is_not_reported(tmp_path):
    """Nothing to fix, so nothing is said."""
    project = _project(tmp_path, {"requirements.txt": "requests>=2.22.0\n"})
    _venv(project, ["requests"])

    findings = _by_id(check_project_dependencies(project))

    assert "dependencies.not_installed" not in findings, findings


def test_a_project_without_a_virtual_environment_is_reported(tmp_path):
    """There is nowhere for the dependencies to live yet."""
    project = _project(tmp_path, {"requirements.txt": "requests\n"})

    findings = _by_id(check_project_dependencies(project))

    assert "dependencies.venv_missing" in findings, findings


def test_an_import_that_nothing_provides_is_reported(tmp_path):
    """The error the user will hit later, found now."""
    project = _project(tmp_path, {"main.py": "import yaml\n"})
    _venv(project)

    findings = _by_id(check_project_dependencies(project))

    assert "dependencies.import_missing" in findings, findings
    assert "yaml" in findings["dependencies.import_missing"].detail


def test_a_relative_import_whose_file_is_missing_is_reported(tmp_path):
    """A file the project should have but does not."""
    project = _project(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": "from .gone import thing\n"})
    _venv(project)

    findings = _by_id(check_project_dependencies(project))

    assert "dependencies.import_broken" in findings, findings
    assert "gone" in findings["dependencies.import_broken"].detail


def test_a_missing_node_package_is_reported(tmp_path):
    """package.json is the manifest, node_modules is what is there."""
    project = _project(
        tmp_path,
        {"package.json": json.dumps({"dependencies": {"lodash": "^4.17.0"}})},
    )
    _venv(project)

    findings = _by_id(check_project_dependencies(project))

    assert "node.not_installed" in findings, findings
    assert "lodash" in findings["node.not_installed"].detail


def test_a_dependency_that_cannot_be_installed_is_reported(tmp_path):
    """A git dependency cannot be installed by pip, so the user is told."""
    project = _project(
        tmp_path,
        {"pyproject.toml": '[tool.poetry.dependencies]\nfoo = {git = "https://example.com/foo.git"}\n'},
    )
    _venv(project)

    findings = _by_id(check_project_dependencies(project))

    assert "dependencies.skipped" in findings, findings
    assert "foo" in findings["dependencies.skipped"].detail


def test_a_project_with_nothing_missing_is_reported_as_ok(tmp_path):
    """The good case still gets a line, so the report is not empty."""
    project = _project(tmp_path, {"main.py": "print('hello')\n"})
    _venv(project)

    findings = _by_id(check_project_dependencies(project))

    assert findings["dependencies.ok"].status == "ok", findings


def test_the_report_reads_as_plain_text(tmp_path):
    """The terminal has no colours, so the report is written out."""
    from doctor.dependencies import format_findings

    project = _project(tmp_path, {"requirements.txt": "requests\n"})
    _venv(project)

    text = format_findings(check_project_dependencies(project))

    assert "requests" in text, text
    assert "❌" in text, text
    assert "→" in text, text


def test_the_report_names_the_package_when_it_differs_from_the_module(tmp_path):
    """'import yaml' is provided by PyYAML, and the user has to know that."""
    project = _project(tmp_path, {"main.py": "import yaml\n"})
    _venv(project)

    findings = _by_id(check_project_dependencies(project))

    assert "PyYAML" in findings["dependencies.import_missing"].detail


def test_the_report_does_not_say_the_same_list_twice(tmp_path):
    """When the module and the package have the same name, one list is enough."""
    project = _project(tmp_path, {"main.py": "import requests\n"})
    _venv(project)

    findings = _by_id(check_project_dependencies(project))

    assert findings["dependencies.import_missing"].detail == "requests cannot be imported."


def test_a_makefile_with_no_make_installed_is_reported(tmp_path, monkeypatch):
    """The project has targets to run, but there is nothing to run them with."""
    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    project = _project(tmp_path, {"Makefile": "build:\n\techo hi\n"})
    _venv(project)

    findings = _by_id(check_project_dependencies(project))

    assert "make.not_installed" in findings, findings
    assert "Makefile" in findings["make.not_installed"].detail


def test_a_makefile_with_make_installed_is_not_reported(tmp_path, monkeypatch):
    """Nothing is wrong, so nothing is said."""
    monkeypatch.setattr(tooling.shutil, "which", lambda name: "C:/tools/make.exe")
    project = _project(tmp_path, {"Makefile": "build:\n\techo hi\n"})
    _venv(project)

    findings = _by_id(check_project_dependencies(project))

    assert "make.not_installed" not in findings, findings
