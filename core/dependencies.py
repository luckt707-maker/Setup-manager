"""Reading a project's declared dependencies and finding the ones that are missing.

The Setup Manager used to know about ``requirements.txt`` and nothing else,
so a Poetry project looked like it had no dependencies at all. This module
reads the places a project can declare them - ``requirements.txt``,
``pyproject.toml`` (both the Poetry table and the standard ``[project]``
table) and ``package.json`` - and works out which of those dependencies are
not actually installed.

It also reads the project's own imports, so a missing package and a missing
file in the tree can be told apart:

* a **missing dependency** is a module the code imports that no environment
  provides;
* a **broken import** is a relative or local import that points at a file
  which is not in the tree.

Standard library only, like the rest of ``core``.
"""
from __future__ import annotations

import ast
import json
import re
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: Folders that are not part of the project's own source.
SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    ".tox",
    ".nox",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
}

#: A module name and the package that provides it. pip installs packages,
#: not modules, so "import yaml" needs "pip install PyYAML".
MODULE_PACKAGES: Dict[str, str] = {
    "attr": "attrs",
    "attrs": "attrs",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "docx": "python-docx",
    "dotenv": "python-dotenv",
    "fitz": "PyMuPDF",
    "jwt": "PyJWT",
    "lxml": "lxml",
    "magic": "python-magic",
    "MySQLdb": "mysqlclient",
    "nacl": "PyNaCl",
    "OpenSSL": "pyOpenSSL",
    "PIL": "Pillow",
    "pkg_resources": "setuptools",
    "psycopg2": "psycopg2-binary",
    "pptx": "python-pptx",
    "serial": "pyserial",
    "sklearn": "scikit-learn",
    "win32api": "pywin32",
    "yaml": "PyYAML",
}

#: The Poetry constraint markers that need converting for pip.
_CARET = "^"
_TILDE = "~"

_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[(?P<extras>[^\]]*)\])?\s*(?P<spec>.*)$"
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def normalize_name(name: str) -> str:
    """The way pip compares distribution names (PEP 503)."""
    return re.sub(r"[-_.]+", "-", str(name)).strip().lower()


def package_for_module(module: str) -> str:
    """The pip package that provides a module."""
    if module in MODULE_PACKAGES:
        return MODULE_PACKAGES[module]
    for known, package in MODULE_PACKAGES.items():
        if known.lower() == module.lower():
            return package
    return module


def _stdlib_names() -> Set[str]:
    """The modules that come with Python itself."""
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return set(names)
    # Python 3.8 and 3.9 do not publish the list, so fall back to the
    # modules a project imports most.
    return {
        "abc", "argparse", "ast", "asyncio", "base64", "collections", "contextlib",
        "copy", "csv", "dataclasses", "datetime", "enum", "functools", "glob",
        "hashlib", "html", "http", "importlib", "inspect", "io", "itertools", "json",
        "logging", "math", "os", "pathlib", "pickle", "platform", "random", "re",
        "shutil", "socket", "sqlite3", "ssl", "string", "subprocess", "sys",
        "tempfile", "textwrap", "threading", "time", "traceback", "typing", "unittest",
        "urllib", "uuid", "warnings", "weakref", "xml", "zipfile",
    }


# --------------------------------------------------------------------------- #
# Declared dependencies
# --------------------------------------------------------------------------- #
@dataclass
class Requirement:
    """One declared dependency."""

    name: str
    specifier: str = ""
    source: str = ""
    extras: Tuple[str, ...] = ()

    def to_pip(self) -> str:
        """The string pip would take."""
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        return f"{self.name}{extras}{self.specifier}"


@dataclass
class Skipped:
    """A declared dependency that could not be turned into a pip install."""

    name: str
    reason: str
    source: str = ""


@dataclass
class Declared:
    """Everything a project declares, and everything that was left out."""

    requirements: List[Requirement] = field(default_factory=list)
    skipped: List[Skipped] = field(default_factory=list)

    def names(self) -> List[str]:
        return [req.name for req in self.requirements]

    def normalized_names(self) -> Set[str]:
        return {normalize_name(req.name) for req in self.requirements}


def _caret_range(version: str) -> Optional[str]:
    """'^2.2.1' means '>=2.2.1,<3.0.0'."""
    parts = version.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    while len(numbers) < 3:
        numbers.append(0)
    major, minor, patch = numbers[0], numbers[1], numbers[2]
    if major > 0:
        upper = f"{major + 1}.0.0"
    elif minor > 0:
        upper = f"0.{minor + 1}.0"
    else:
        upper = f"0.0.{patch + 1}"
    return f">={major}.{minor}.{patch},<{upper}"


def _tilde_range(version: str) -> Optional[str]:
    """'~2.22.0' means '>=2.22.0,<2.23.0'."""
    parts = version.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    while len(numbers) < 3:
        numbers.append(0)
    major, minor, patch = numbers[0], numbers[1], numbers[2]
    if len(parts) == 1:
        upper = f"{major + 1}.0.0"
    else:
        upper = f"{major}.{minor + 1}.0"
    return f">={major}.{minor}.{patch},<{upper}"


def poetry_constraint(spec: str) -> Optional[str]:
    """Turn a Poetry version constraint into a pip specifier."""
    text = str(spec).strip()
    if not text or text == "*":
        return ""
    if text.startswith(_CARET):
        return _caret_range(text[1:].strip())
    if text.startswith(_TILDE):
        return _tilde_range(text[1:].strip())
    if text.startswith("="):
        return "==" + text[1:].strip()
    if text.startswith((">=", "<=", "!=", ">", "<", ",")):
        return text
    if _VERSION_RE.match(text):
        return f"=={text}"
    if text.endswith(".*"):
        return f"=={text}"
    return None


def _load_toml(path: Path) -> Dict:
    """Read a TOML file, or nothing at all when it cannot be read."""
    try:
        import tomllib
    except ImportError:  # Python 3.10 and older
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError):
        return {}


def _parse_requirement_line(line: str, source: str) -> Tuple[Optional[Requirement], Optional[Skipped]]:
    """Read one 'name[extras]specifier' line."""
    text = line.split("#", 1)[0].strip()
    if not text:
        return None, None
    if text.startswith("-"):
        name = text.split()[0]
        return None, Skipped(name, "it is not a plain package name (an option or a reference)", source)
    name_part, _, spec = text.partition(";")
    match = _REQUIREMENT_RE.match(name_part.strip())
    if not match:
        return None, Skipped(name_part.strip() or text, "the name could not be read", source)
    name = match.group("name")
    extras = tuple(part.strip() for part in (match.group("extras") or "").split(",") if part.strip())
    specifier = (match.group("spec") or "").strip() + ("," + spec.strip() if spec.strip() else "")
    specifier = re.sub(r"\s+", "", specifier)
    return Requirement(name=name, specifier=specifier, source=source, extras=extras), None


# Files that name what the program needs in order to run. A requirements
# folder often also holds dev, test, and docs lists; those are not installed.
_RUNTIME_REQUIREMENT_NAMES = {
    "base.txt", "prod.txt", "production.txt", "main.txt", "runtime.txt",
    "default.txt", "requirements.txt", "app.txt",
}
_DEV_REQUIREMENT_PARTS = ("dev", "develop", "development", "test", "tests", "testing", "doc", "docs", "ci", "lint", "style", "optional", "extra")


def _is_dev_extra(name: str) -> bool:
    """True for a Poetry group or requirements file that is not needed to run."""
    key = Path(str(name)).stem.lower().replace("_", "-")
    parts = [part for part in key.split("-") if part]
    return any(part in _DEV_REQUIREMENT_PARTS for part in parts)


def runtime_requirement_files(root: Path) -> List[Path]:
    """Requirement files that describe how to run the program.

    The project root ``requirements.txt`` counts. So does a runtime file
    inside ``requirements/`` or ``requirement/``. Dev and test lists do not.
    """
    root = Path(root)
    files: List[Path] = []
    root_file = root / "requirements.txt"
    if root_file.is_file():
        files.append(root_file)
    for folder_name in ("requirements", "requirement"):
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.txt")):
            if path.name.lower() in _RUNTIME_REQUIREMENT_NAMES and not _is_dev_extra(path.name):
                files.append(path)
    return files


def _read_requirement_file(path: Path, source: str) -> Tuple[List[Requirement], List[Skipped]]:
    requirements: List[Requirement] = []
    skipped: List[Skipped] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    for line in text.splitlines():
        requirement, skip = _parse_requirement_line(line, source)
        if requirement is not None:
            requirements.append(requirement)
        elif skip is not None:
            skipped.append(skip)
    return requirements, skipped


def _file_pulls_other_files(path: Path) -> bool:
    """True when pip would open another file, which might be a dev list."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith(("-r", "--requirement", "-c", "--constraint")):
            return True
    return False


def _requirements_txt(root: Path) -> Tuple[List[Requirement], List[Skipped]]:
    """Read the runtime requirement files, and record the ones left out."""
    requirements: List[Requirement] = []
    skipped: List[Skipped] = []
    for path in runtime_requirement_files(root):
        source = path.relative_to(root).as_posix()
        found, left_out = _read_requirement_file(path, source)
        requirements.extend(found)
        skipped.extend(left_out)
    for folder_name in ("requirements", "requirement"):
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.txt")):
            if path in runtime_requirement_files(root):
                continue
            skipped.append(Skipped(
                path.name,
                "it is not part of what the program needs to run, so it is not installed",
                path.relative_to(root).as_posix(),
            ))
    return requirements, skipped


def _poetry_requirements(root: Path) -> Tuple[List[Requirement], List[Skipped]]:
    """Read the Poetry tables in pyproject.toml."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return [], []
    data = _load_toml(path)
    poetry = (data.get("tool") or {}).get("poetry") or {}
    if not poetry:
        return [], []

    groups: List[Dict] = []
    main = poetry.get("dependencies")
    if isinstance(main, dict):
        groups.append(main)
    requirements: List[Requirement] = []
    skipped: List[Skipped] = []
    for group_name, group in (poetry.get("group") or {}).items():
        if not isinstance(group, dict) or not isinstance(group.get("dependencies"), dict):
            continue
        # dev, test, and ci groups are for developing the program, not running it.
        if _is_dev_extra(str(group_name)):
            for name in group["dependencies"]:
                if str(name).strip().lower() == "python":
                    continue
                skipped.append(Skipped(
                    str(name),
                    "it is a development dependency and is not needed to run the program",
                    f"pyproject.toml [tool.poetry.group.{group_name}]",
                ))
            continue
        groups.append(group["dependencies"])

    for group in groups:
        for name, spec in group.items():
            if str(name).strip().lower() == "python":
                # A version constraint on the interpreter, not a package.
                continue
            source = "pyproject.toml [tool.poetry.dependencies]"
            requirement, skip = _poetry_requirement(str(name), spec, source)
            if requirement is not None:
                requirements.append(requirement)
            elif skip is not None:
                skipped.append(skip)
    return requirements, skipped


def _poetry_requirement(
    name: str, spec, source: str
) -> Tuple[Optional[Requirement], Optional[Skipped]]:
    """Turn one Poetry dependency into a pip requirement, or explain why not."""
    if isinstance(spec, str):
        constraint = poetry_constraint(spec)
        if constraint is None:
            return None, Skipped(name, f"the version constraint {spec!r} could not be read", source)
        return Requirement(name=name, specifier=constraint, source=source), None

    if isinstance(spec, list):
        parts: List[str] = []
        for item in spec:
            if not isinstance(item, str):
                return None, Skipped(name, "part of the version constraint is not a version", source)
            constraint = poetry_constraint(item)
            if constraint is None:
                return None, Skipped(name, f"the version constraint {item!r} could not be read", source)
            if constraint:
                parts.append(constraint)
        return Requirement(name=name, specifier=",".join(parts), source=source), None

    if isinstance(spec, dict):
        if "git" in spec:
            return None, Skipped(name, "it comes from a git repository, so pip cannot install it here", source)
        if "path" in spec:
            return None, Skipped(name, "it comes from a local path, so pip cannot install it here", source)
        if "url" in spec:
            return None, Skipped(name, "it comes from a URL, so pip cannot install it here", source)
        if spec.get("optional"):
            return None, Skipped(name, "it is an optional dependency and is not installed by default", source)
        for marker in ("python", "sys_platform", "platform_system", "platform_machine", "implementation_name"):
            if marker in spec:
                return None, Skipped(
                    name,
                    f"it only applies to some environments (the {marker} marker), so it is not installed for everyone",
                    source,
                )
        version = spec.get("version")
        if version is None:
            return None, Skipped(name, "the dependency could not be read", source)
        constraint = poetry_constraint(version)
        if constraint is None:
            return None, Skipped(name, f"the version constraint {version!r} could not be read", source)
        return Requirement(name=name, specifier=constraint, source=source), None

    return None, Skipped(name, "the dependency could not be read", source)


def _pep621_requirements(root: Path) -> Tuple[List[Requirement], List[Skipped]]:
    """Read the standard [project] table in pyproject.toml."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return [], []
    data = _load_toml(path)
    project = data.get("project") or {}
    if not isinstance(project, dict):
        return [], []

    lines: List[str] = []
    dependencies = project.get("dependencies")
    if isinstance(dependencies, list):
        lines.extend(str(line) for line in dependencies)

    requirements: List[Requirement] = []
    skipped: List[Skipped] = []
    # Optional extras are installed only when someone asks for them.
    for group_name, group in (project.get("optional-dependencies") or {}).items():
        if not isinstance(group, list):
            continue
        for line in group:
            requirement, _skip = _parse_requirement_line(str(line), f"pyproject.toml [project.optional-dependencies.{group_name}]")
            label = requirement.name if requirement is not None else str(line).strip()
            if label:
                skipped.append(Skipped(
                    label,
                    "it is an optional extra and is not needed to run the program",
                    f"pyproject.toml [project.optional-dependencies.{group_name}]",
                ))
    for line in lines:
        requirement, skip = _parse_requirement_line(line, "pyproject.toml [project]")
        if requirement is not None:
            requirements.append(requirement)
        elif skip is not None:
            skipped.append(skip)
    return requirements, skipped


def declared_requirements(root: Path) -> Declared:
    """Everything a project declares, read from every place it can declare it."""
    root = Path(root)
    requirements: List[Requirement] = []
    skipped: List[Skipped] = []

    for reader in (_requirements_txt, _poetry_requirements, _pep621_requirements):
        found, left_out = reader(root)
        requirements.extend(found)
        skipped.extend(left_out)

    # The same package can be declared twice; the first mention wins, but a
    # later one with a tighter constraint is not thrown away silently.
    unique: Dict[str, Requirement] = {}
    for requirement in requirements:
        key = normalize_name(requirement.name)
        if key not in unique:
            unique[key] = requirement
    return Declared(requirements=list(unique.values()), skipped=skipped)


def install_arguments(root: Path) -> List[str]:
    """The pip arguments that install what the program needs to run.

    A requirements file is passed with ``-r`` when it does not point at
    another file. Development lists and optional extras are omitted.
    """
    root = Path(root)
    arguments: List[str] = []
    used_sources = set()
    for path in runtime_requirement_files(root):
        source = path.relative_to(root).as_posix()
        if _file_pulls_other_files(path):
            continue
        arguments.extend(["-r", source])
        used_sources.add(source)
    for requirement in declared_requirements(root).requirements:
        if requirement.source in used_sources:
            continue
        arguments.append(requirement.to_pip())
    return arguments


# --------------------------------------------------------------------------- #
# What is installed
# --------------------------------------------------------------------------- #
def find_venv(root: Path) -> Optional[Path]:
    """The project's own virtual environment, if it has one."""
    root = Path(root)
    for name in ("venv", ".venv", "env", ".env"):
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


def site_packages_dirs(venv: Optional[Path]) -> List[Path]:
    """The site-packages folders of a virtual environment."""
    if venv is None:
        return []
    venv = Path(venv)
    found: List[Path] = []
    windows = venv / "Lib" / "site-packages"
    if windows.is_dir():
        found.append(windows)
    for candidate in sorted(venv.glob("lib/python*/site-packages")):
        if candidate.is_dir():
            found.append(candidate)
    return found


def interpreter_site_packages() -> List[Path]:
    """The site-packages of the interpreter running Setup Manager."""
    found: List[Path] = []
    purelib = sysconfig.get_paths().get("purelib")
    if purelib:
        found.append(Path(purelib))
    return found


def installed_distributions(site_dirs: Iterable[Path]) -> Set[str]:
    """The distribution names installed in a set of site-packages folders."""
    names: Set[str] = set()
    for directory in site_dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.endswith(".dist-info") or name.endswith(".egg-info"):
                # 'requests-2.31.0.dist-info' -> 'requests'
                stem = name.rsplit(".dist-info", 1)[0].rsplit(".egg-info", 1)[0]
                if "-" in stem:
                    stem = stem.rsplit("-", 1)[0]
                if stem:
                    names.add(normalize_name(stem))
    return names


def importable_names(site_dirs: Iterable[Path]) -> Set[str]:
    """The top-level names a set of site-packages folders makes importable."""
    names: Set[str] = set()
    for directory in site_dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.endswith(".dist-info") or name.endswith(".egg-info") or name.endswith(".pth"):
                continue
            if entry.is_dir():
                names.add(name)
            elif name.endswith(".py"):
                names.add(name[:-3])
    return names


# A child folder with its own manifest is a different program. Sherlock's
# ``.actor`` tree imports packages Sherlock itself does not use.
_NESTED_PROJECT_MARKERS = (
    "package.json", "go.mod", "pyproject.toml", "requirements.txt", "Pipfile",
    "Cargo.toml", "composer.json", "Gemfile", "pom.xml", "build.gradle",
    "build.gradle.kts", "Package.swift", "setup.py",
)


def _is_separate_project(path: Path) -> bool:
    return any((path / name).is_file() for name in _NESTED_PROJECT_MARKERS)


# --------------------------------------------------------------------------- #
# The project's own source
# --------------------------------------------------------------------------- #
def _python_files(root: Path) -> List[Path]:
    """Python files that belong to this program.

    Environment folders, hidden folders, and a child program with its own
    manifest are left out.
    """
    found: List[Path] = []

    def walk(here: Path) -> None:
        try:
            entries = list(here.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                name = entry.name
                if name in SKIP_DIRECTORIES or name.startswith("."):
                    continue
                if _is_separate_project(entry):
                    continue
                walk(entry)
            elif entry.suffix == ".py":
                found.append(entry)

    walk(Path(root))
    return found


def _local_names(root: Path) -> Set[str]:
    """The top-level names the project itself provides."""
    names: Set[str] = set()
    for base in (Path(root), Path(root) / "src"):
        if not base.is_dir():
            continue
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and (entry / "__init__.py").is_file():
                names.add(entry.name)
            elif entry.is_file() and entry.suffix == ".py":
                names.add(entry.stem)
    return names


def _project_module_names(root: Path) -> Set[str]:
    """Every module name the project provides, at any depth.

    A test helper that lives in ``tests/`` is still the project's own file.
    Only the top of the tree is importable without help, but a name the
    project has somewhere in it is not a package that is missing.
    """
    names: Set[str] = {path.stem for path in _python_files(root)}
    for path in _python_files(root):
        for parent in path.parents:
            if parent == root:
                break
            if (parent / "__init__.py").is_file():
                names.add(parent.name)
    return names


def _module_exists(base: Path, dotted: str) -> bool:
    """Whether 'base/dotted' names a module file or a package."""
    if not dotted:
        return (base / "__init__.py").is_file()
    parts = dotted.split(".")
    candidate = base.joinpath(*parts)
    return candidate.with_suffix(".py").is_file() or (candidate / "__init__.py").is_file()


@dataclass
class MissingImport:
    """An import that nothing provides."""

    module: str
    package: str
    source: Path
    line: int = 0


@dataclass
class BrokenImport:
    """An import that points at a file which is not in the project."""

    module: str
    source: Path
    line: int = 0
    reason: str = ""


@dataclass
class ImportScan:
    """What a project's imports say about what is missing."""

    missing_imports: List[MissingImport] = field(default_factory=list)
    broken_imports: List[BrokenImport] = field(default_factory=list)

    def packages(self) -> List[str]:
        """The pip names of the missing imports, without repeats."""
        seen: List[str] = []
        for item in self.missing_imports:
            if item.package not in seen:
                seen.append(item.package)
        return seen


def scan_imports(
    root: Path,
    available: Set[str],
    declared: Optional[Set[str]] = None,
    accepted: Optional[Set[str]] = None,
) -> ImportScan:
    """Read a project's imports and work out what is missing.

    ``available`` is the set of names that can be imported right now. A name
    the project provides itself is never missing. ``accepted`` names are
    declared on purpose as development or optional packages, so a test
    import is not reported as something the program needs.
    """
    root = Path(root)
    available = set(available or ())
    declared = {normalize_name(name) for name in (declared or set())}
    accepted = {normalize_name(name) for name in (accepted or set())}
    stdlib = _stdlib_names()
    local = _local_names(root)
    project_names = _project_module_names(root)
    scan = ImportScan()

    for path in _python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _check_absolute(
                        root, path, node, alias.name, available, declared, accepted,
                        stdlib, local, project_names, scan,
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    _check_relative(root, path, node, scan)
                elif node.module:
                    _check_absolute(
                        root, path, node, node.module, available, declared, accepted,
                        stdlib, local, project_names, scan,
                    )
    return scan


def _check_absolute(
    root: Path,
    path: Path,
    node,
    dotted: str,
    available: Set[str],
    declared: Set[str],
    accepted: Set[str],
    stdlib: Set[str],
    local: Set[str],
    project_names: Set[str],
    scan: ImportScan,
) -> None:
    """Check one 'import x' or 'from x import y'."""
    top = dotted.split(".")[0]
    if not top:
        return
    if top in stdlib or top in available:
        return
    if normalize_name(top) in accepted or normalize_name(package_for_module(top)) in accepted:
        return
    if top in local:
        # The project's own package: the file it names has to be there.
        if not _module_exists(root, dotted):
            scan.broken_imports.append(
                BrokenImport(
                    module=dotted,
                    source=path,
                    line=getattr(node, "lineno", 0),
                    reason="the project has no module by that name",
                )
            )
        return
    if normalize_name(top) in declared:
        # It is a declared dependency that is simply not installed.
        package = package_for_module(top)
        if package not in [item.package for item in scan.missing_imports]:
            scan.missing_imports.append(
                MissingImport(module=top, package=package, source=path, line=getattr(node, "lineno", 0))
            )
        return
    if top in project_names:
        # The project has a file by that name somewhere in its tree, so it
        # is the project's own module and not a package to install.
        return
    package = package_for_module(top)
    if package not in [item.package for item in scan.missing_imports]:
        scan.missing_imports.append(
            MissingImport(module=top, package=package, source=path, line=getattr(node, "lineno", 0))
        )


def _check_relative(root: Path, path: Path, node, scan: ImportScan) -> None:
    """Check a relative import: the file it names has to be in the tree."""
    relative = path.relative_to(root)
    parts = relative.parts
    if len(parts) < 2:
        # A file at the top of the tree has no package to be relative to.
        base = root
    else:
        base = path.parent
        for _ in range(node.level - 1):
            base = base.parent
    module = node.module or ""
    if not _module_exists(base, module):
        name = "." * node.level + module
        scan.broken_imports.append(
            BrokenImport(
                module=name,
                source=path,
                line=getattr(node, "lineno", 0),
                reason="the file it points at is not in the project",
            )
        )


# --------------------------------------------------------------------------- #
# Node
# --------------------------------------------------------------------------- #
def node_dependencies(root: Path) -> Tuple[List[str], List[str]]:
    """The Node packages a project declares, and the ones that are missing."""
    root = Path(root)
    manifest = root / "package.json"
    if not manifest.is_file():
        return [], []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return [], []
    if not isinstance(data, dict):
        return [], []

    declared: List[str] = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        entries = data.get(section)
        if isinstance(entries, dict):
            for name in entries:
                if name not in declared:
                    declared.append(str(name))

    installed = root / "node_modules"
    missing: List[str] = []
    for name in declared:
        if not (installed / name).exists():
            missing.append(name)
    return declared, missing
