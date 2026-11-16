"""What a project folder is missing.

:mod:`doctor.diagnostics` answers "is this machine set up correctly?". This
module answers the question about a *project*: the packages it declares
that are not installed, the modules it imports that nothing provides, the
files it points at that are not there, and the Node packages it has not
installed.

Everything is read from the project folder and its virtual environment.
Nothing is installed and nothing is run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from core.dependencies import (
    declared_requirements,
    find_venv,
    importable_names,
    installed_distributions,
    interpreter_site_packages,
    node_dependencies,
    normalize_name,
    scan_imports,
    site_packages_dirs,
)
from core.tooling import find_makefile, make_command

from .diagnostics import FAIL, OK, WARN, Diagnostic, required_tools


def _one_or_many(count: int) -> str:
    """'is' for one thing, 'are' for more."""
    return "is" if count == 1 else "are"


def _left_out_on_purpose(reason: str) -> bool:
    """True when a dependency was skipped because the program does not need it."""
    text = reason.lower()
    return (
        "not needed to run" in text
        or "not part of what the program needs" in text
        or "not installed by default" in text
        or "not installed for everyone" in text
    )


def check_project_dependencies(root: Path) -> List[Diagnostic]:
    """Report the dependencies and files a project is missing."""
    root = Path(root)
    findings: List[Diagnostic] = []

    declared = declared_requirements(root)

    venv = find_venv(root)
    if venv is None:
        site_dirs = interpreter_site_packages()
        findings.append(
            Diagnostic(
                id="dependencies.venv_missing",
                title="Virtual environment",
                status=WARN,
                detail=f"{root} has no virtual environment, so its dependencies cannot be installed.",
                fix="Run 'Set up Python' to create one and install the dependencies.",
            )
        )
    else:
        site_dirs = site_packages_dirs(venv)
        findings.append(
            Diagnostic(
                id="dependencies.venv",
                title="Virtual environment",
                status=OK,
                detail=str(venv),
                data={"path": str(venv)},
            )
        )

    installed = installed_distributions(site_dirs)
    available = importable_names(site_dirs)

    missing = [
        requirement
        for requirement in declared.requirements
        if normalize_name(requirement.name) not in installed
    ]
    if missing:
        names = ", ".join(requirement.name for requirement in missing)
        findings.append(
            Diagnostic(
                id="dependencies.not_installed",
                title="Declared dependencies that are not installed",
                status=FAIL,
                detail=f"{names} {_one_or_many(len(missing))} declared but not installed.",
                fix="Run 'Set up Python' to install the project's dependencies.",
                data={"packages": [requirement.name for requirement in missing]},
            )
        )

    left_out = [item for item in declared.skipped if _left_out_on_purpose(item.reason)]
    blocked = [item for item in declared.skipped if item not in left_out]
    if left_out:
        names = ", ".join(item.name for item in left_out)
        findings.append(
            Diagnostic(
                id="dependencies.not_needed",
                title="Left out, not needed to run",
                status=OK,
                detail=names,
                data={"packages": [item.name for item in left_out]},
            )
        )
    if blocked:
        detail = "; ".join(f"{item.name}: {item.reason}" for item in blocked)
        findings.append(
            Diagnostic(
                id="dependencies.skipped",
                title="Dependencies that could not be installed automatically",
                status=WARN,
                detail=detail,
                fix="Install these by hand, or change how they are declared.",
                data={"skipped": [{"name": item.name, "reason": item.reason} for item in blocked]},
            )
        )

    accepted = {normalize_name(item.name) for item in left_out}
    scan = scan_imports(root, available, declared.normalized_names(), accepted)
    if scan.missing_imports:
        modules = ", ".join(item.module for item in scan.missing_imports)
        packages = ", ".join(scan.packages())
        # 'import yaml' is provided by PyYAML, so the package name is worth
        # saying - but not when it is the same name again.
        if packages == modules:
            detail = f"{modules} cannot be imported."
        else:
            detail = f"{modules} cannot be imported. The package that provides it: {packages}."
        findings.append(
            Diagnostic(
                id="dependencies.import_missing",
                title="Modules the project imports that nothing provides",
                status=FAIL,
                detail=detail,
                fix=(
                    f"Install it (for example: pip install {packages}), "
                    "or add the missing file to the project."
                ),
                data={
                    "modules": [item.module for item in scan.missing_imports],
                    "packages": scan.packages(),
                },
            )
        )
    if scan.broken_imports:
        detail = "; ".join(
            f"{item.source.name} line {item.line}: {item.module} ({item.reason})"
            for item in scan.broken_imports
        )
        findings.append(
            Diagnostic(
                id="dependencies.import_broken",
                title="Imports that point at files that are not there",
                status=FAIL,
                detail=detail,
                fix="Create the file, or fix the import.",
                data={
                    "imports": [
                        {
                            "module": item.module,
                            "file": str(item.source),
                            "line": item.line,
                            "reason": item.reason,
                        }
                        for item in scan.broken_imports
                    ]
                },
            )
        )

    node_declared, node_missing = node_dependencies(root)
    # A package.json that only lists unused names is not a Node program.
    if node_missing and "node" in required_tools(root):
        names = ", ".join(node_missing)
        findings.append(
            Diagnostic(
                id="node.not_installed",
                title="Node packages that are not installed",
                status=FAIL,
                detail=f"{names} {_one_or_many(len(node_missing))} in package.json but not in node_modules.",
                fix="Run 'Set up Node.js' to install them.",
                data={"packages": node_missing, "declared": node_declared},
            )
        )

    # A Makefile lists the targets, but something has to run them.
    if find_makefile(root) is not None and make_command() is None:
        findings.append(
            Diagnostic(
                id="make.not_installed",
                title="Make is not installed",
                status=WARN,
                detail=(
                    "This project has a Makefile, but there is no make program "
                    "to run its targets."
                ),
                fix=(
                    "Install Make (for example: winget install GnuWin32.Make), "
                    "then the Makefile targets can be run."
                ),
            )
        )

    problems = [finding for finding in findings if finding.status in (WARN, FAIL)]
    if not problems:
        findings.append(
            Diagnostic(
                id="dependencies.ok",
                title="Project dependencies",
                status=OK,
                detail="Everything the project declares is installed, and every import resolves.",
            )
        )
    return findings


#: What each status looks like in a terminal.
STATUS_MARKS = {OK: "✅", WARN: "⚠️", FAIL: "❌"}


def format_findings(findings: List[Diagnostic]) -> str:
    """The report as plain text, for a terminal."""
    lines: List[str] = []
    for finding in findings:
        mark = STATUS_MARKS.get(finding.status, "•")
        lines.append(f"{mark} {finding.title}")
        if finding.detail:
            lines.append(f"   {finding.detail}")
        if finding.fix:
            lines.append(f"   → {finding.fix}")
    return "\n".join(lines)
