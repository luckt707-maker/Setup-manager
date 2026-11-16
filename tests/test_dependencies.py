"""Reading a project's dependencies and finding the ones that are missing.

The Setup Manager could only read requirements.txt, so a Poetry project
looked like it had no dependencies at all, and nothing ever told the user
which package was missing when an import failed.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.dependencies import (  # noqa: E402
    declared_requirements,
    node_dependencies,
    scan_imports,
)

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

PEP621_PROJECT = """
[project]
name = "demo"
version = "0.1.0"
dependencies = [
    "requests>=2.22.0",
    "flask",
]
"""


def _project(tmp_path, files):
    project = tmp_path / "proj"
    project.mkdir()
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


# --------------------------------------------------------------------------- #
# Declared dependencies
# --------------------------------------------------------------------------- #
def test_a_poetry_project_declares_its_packages(tmp_path):
    """Poetry keeps them in pyproject.toml, so that is where they are read."""
    project = _project(tmp_path, {"pyproject.toml": POETRY_PROJECT})

    declared = declared_requirements(project)

    assert "tomli" in declared.names(), declared.names()
    assert "requests" in declared.names(), declared.names()
    assert "pytest" not in declared.names(), declared.names()
    assert any(item.name == "pytest" and "not needed to run" in item.reason for item in declared.skipped)


def test_the_python_constraint_is_not_a_package(tmp_path):
    """'python = "^3.9"' says which interpreter to use, not what to install."""
    project = _project(tmp_path, {"pyproject.toml": POETRY_PROJECT})

    declared = declared_requirements(project)

    assert "python" not in declared.names(), declared.names()


def test_a_poetry_caret_becomes_a_pip_range(tmp_path):
    """'^2.2.1' means 'at least 2.2.1, but not 3'."""
    project = _project(tmp_path, {"pyproject.toml": POETRY_PROJECT})

    by_name = {req.name: req for req in declared_requirements(project).requirements}

    assert by_name["tomli"].specifier == ">=2.2.1,<3.0.0"
    assert by_name["colorama"].specifier == ">=0.4.1,<0.5.0"
    assert by_name["requests"].specifier == ">=2.22.0"


def test_a_poetry_tilde_becomes_a_pip_range(tmp_path):
    """'~2.22.0' allows 2.22.x but not 2.23."""
    project = _project(
        tmp_path,
        {"pyproject.toml": '[tool.poetry.dependencies]\npython = "^3.9"\nrequests = "~2.22.0"\n'},
    )

    by_name = {req.name: req for req in declared_requirements(project).requirements}

    assert by_name["requests"].specifier == ">=2.22.0,<2.23.0"


def test_a_pep621_project_declares_its_packages(tmp_path):
    """The standard [project] table is what newer tools write."""
    project = _project(tmp_path, {"pyproject.toml": PEP621_PROJECT})

    declared = declared_requirements(project)
    by_name = {req.name: req for req in declared.requirements}

    assert "requests" in declared.names(), declared.names()
    assert "flask" in declared.names(), declared.names()
    assert by_name["requests"].specifier == ">=2.22.0"


def test_a_requirements_folder_installs_only_the_runtime_list(tmp_path):
    """dev.txt and test.txt sit beside base.txt and must not be downloaded."""
    project = _project(tmp_path, {
        "requirements/base.txt": "requests>=2.22.0\n",
        "requirements/dev.txt": "pytest\n",
        "requirements/test.txt": "coverage\n",
        "pyproject.toml": """
[tool.poetry.dependencies]
python = "^3.9"
tomli = "^2.2.1"

[tool.poetry.group.dev.dependencies]
pytest = "^8.4.2"

[tool.poetry.group.ci.dependencies]
defusedxml = "^0.7.1"
""",
    })

    from core.dependencies import install_arguments

    declared = declared_requirements(project)
    arguments = install_arguments(project)

    assert "requests" in declared.names()
    assert "tomli" in declared.names()
    assert "pytest" not in declared.names()
    assert "coverage" not in declared.names()
    assert "defusedxml" not in declared.names()
    assert "-r" in arguments and "requirements/base.txt" in arguments
    assert "dev.txt" not in arguments
    assert not any("pytest" in part for part in arguments)


def test_a_requirements_txt_project_declares_its_packages(tmp_path):
    """The old path must keep working."""
    project = _project(tmp_path, {"requirements.txt": "requests>=2.22.0\n# a comment\nflask==3.0.0\n"})

    declared = declared_requirements(project)
    by_name = {req.name: req for req in declared.requirements}

    assert "requests" in declared.names(), declared.names()
    assert by_name["flask"].specifier == "==3.0.0"


def test_a_dependency_from_a_git_url_is_reported_as_skipped(tmp_path):
    """It cannot be expressed as a pip version, so it is not installed."""
    project = _project(
        tmp_path,
        {"pyproject.toml": '[tool.poetry.dependencies]\nfoo = {git = "https://example.com/foo.git"}\n'},
    )

    declared = declared_requirements(project)

    assert "foo" not in declared.names(), declared.names()
    assert any(item.name == "foo" for item in declared.skipped), declared.skipped
    assert "git" in declared.skipped[0].reason.lower(), declared.skipped[0].reason


def test_a_dependency_with_a_marker_is_reported_as_skipped(tmp_path):
    """A marker means 'only on some interpreters', which is not every one."""
    project = _project(
        tmp_path,
        {"pyproject.toml": '[tool.poetry.dependencies]\ntomli = {version = "^2.0", python = "<3.11"}\n'},
    )

    declared = declared_requirements(project)

    assert "tomli" not in declared.names(), declared.names()
    assert declared.skipped, declared.skipped
    assert "python" in declared.skipped[0].reason.lower(), declared.skipped[0].reason


# --------------------------------------------------------------------------- #
# Imports
# --------------------------------------------------------------------------- #
def test_a_third_party_import_that_is_not_installed_is_reported(tmp_path):
    """'import yaml' fails with a confusing error unless we say what is missing."""
    project = _project(tmp_path, {"main.py": "import requests\nimport yaml\n"})

    scan = scan_imports(project, {"requests"})

    assert [item.module for item in scan.missing_imports] == ["yaml"], scan.missing_imports
    assert scan.missing_imports[0].package == "PyYAML", scan.missing_imports[0]


def test_a_standard_library_import_is_not_reported(tmp_path):
    """os and json come with Python; they are never missing."""
    project = _project(tmp_path, {"main.py": "import os\nimport json\nfrom pathlib import Path\n"})

    scan = scan_imports(project, set())

    assert not scan.missing_imports, scan.missing_imports


def test_a_local_import_is_not_reported_as_missing(tmp_path):
    """The project's own modules are not dependencies."""
    project = _project(
        tmp_path,
        {"pkg/__init__.py": "", "pkg/mod.py": "from .other import thing\n", "pkg/other.py": "thing = 1\n"},
    )

    scan = scan_imports(project, set())

    assert not scan.missing_imports, scan.missing_imports
    assert not scan.broken_imports, scan.broken_imports


def test_a_relative_import_whose_file_is_missing_is_reported(tmp_path):
    """This is the 'missing file' half: the code points at a file that is not there."""
    project = _project(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": "from .gone import thing\n"})

    scan = scan_imports(project, set())

    assert not scan.missing_imports, scan.missing_imports
    assert len(scan.broken_imports) == 1, scan.broken_imports
    assert "gone" in scan.broken_imports[0].module, scan.broken_imports[0]


def test_a_name_the_project_does_not_have_is_reported(tmp_path):
    """'import helper' with no helper.py anywhere in the project.

    A name like this is either a package that is not installed or a file
    that is not in the tree, and the project itself cannot say which. It is
    reported as a missing module, and the report says both are possible.
    """
    project = _project(tmp_path, {"main.py": "import helper\n"})

    scan = scan_imports(project, set())

    assert [item.module for item in scan.missing_imports] == ["helper"], scan.missing_imports
    assert not scan.broken_imports, scan.broken_imports


def test_a_hidden_folder_is_not_part_of_this_program(tmp_path):
    """A side project under a hidden folder has its own imports."""
    project = _project(
        tmp_path,
        {
            "main.py": "print(1)\n",
            ".actor/app/main.py": "import agents\n",
        },
    )

    scan = scan_imports(project, set())

    assert not scan.missing_imports, scan.missing_imports


def test_a_development_package_is_not_reported_as_missing(tmp_path):
    """The test suite may import a package the program does not need."""
    project = _project(tmp_path, {"tests/test_thing.py": "import pytest\n"})

    scan = scan_imports(project, set(), accepted={"pytest"})

    assert not scan.missing_imports, scan.missing_imports


def test_a_module_that_is_installed_is_not_reported(tmp_path):
    """Nothing to fix, so nothing is said."""
    project = _project(tmp_path, {"main.py": "import requests\n"})

    scan = scan_imports(project, {"requests"})

    assert not scan.missing_imports, scan.missing_imports
    assert not scan.broken_imports, scan.broken_imports


# --------------------------------------------------------------------------- #
# Node
# --------------------------------------------------------------------------- #
def test_a_node_dependency_that_is_not_installed_is_reported(tmp_path):
    """package.json says what is needed; node_modules says what is there."""
    project = _project(
        tmp_path,
        {"package.json": json.dumps({"dependencies": {"lodash": "^4.17.0"}})},
    )

    declared, missing = node_dependencies(project)

    assert declared == ["lodash"], declared
    assert missing == ["lodash"], missing


def test_an_installed_node_dependency_is_not_reported(tmp_path):
    """node_modules/lodash is there, so there is nothing to do."""
    project = _project(
        tmp_path,
        {
            "package.json": json.dumps({"dependencies": {"lodash": "^4.17.0"}}),
            "node_modules/lodash/package.json": json.dumps({"name": "lodash"}),
        },
    )

    declared, missing = node_dependencies(project)

    assert declared == ["lodash"], declared
    assert missing == [], missing


def test_a_module_the_project_keeps_in_a_subfolder_is_not_reported(tmp_path):
    """A test helper in tests/ is the project's own file, not a package."""
    project = _project(
        tmp_path,
        {
            "tests/helper.py": "value = 1\n",
            "tests/test_thing.py": "from helper import value\n",
        },
    )

    scan = scan_imports(project, set())

    assert not scan.missing_imports, scan.missing_imports
    assert not scan.broken_imports, scan.broken_imports
