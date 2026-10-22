"""The Setup Manager doctor.

``doctor`` answers two kinds of question:

* *Is this machine set up correctly?* - see :mod:`doctor.diagnostics`.
* *Why did my build fail?* - the knowledge base in
  :mod:`doctor.knowledge_base` turns a wall of compiler output into a named
  cause and a fix.

:mod:`doctor.build_doctor` runs a whole pass and writes a report.

The submodules are imported lazily so that ``python -m doctor.build_doctor``
does not import the module twice, and so importing ``doctor`` stays cheap.
"""
from typing import Any

from core.console import _force_utf8_console

#: Public name -> the submodule it lives in.
_EXPORTS = {
    "BuildDoctor": "build_doctor",
    "Diagnostic": "diagnostics",
    "check_broken_symlinks": "diagnostics",
    "check_project_dependencies": "dependencies",
    "format_findings": "dependencies",
    "check_python_environment": "diagnostics",
    "check_tool_versions": "diagnostics",
    "check_tooling": "diagnostics",
    "check_wsl_config": "diagnostics",
    "check_zone_identifier": "diagnostics",
    "diagnose_build_output": "diagnostics",
    "run_machine_diagnostics": "diagnostics",
    "summarize": "diagnostics",
    "KnowledgeBase": "knowledge_base",
    "DoctorLogManager": "log_manager",
    "Repair": "repairs",
    "apply_repair": "repairs",
    "available_repairs": "repairs",
    "clear_pycache": "repairs",
    "fix_wsl_conf": "repairs",
    "prune_broken_symlinks": "repairs",
    "strip_zone_identifier": "repairs",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Import the owning submodule on first use (PEP 562)."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


# The doctor prints its report on a console that may be cp1252.
_force_utf8_console()
