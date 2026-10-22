"""Setup Manager core engine.

The public API is re-exported here so callers can simply do::

    from core import Setup, CommandExecutor, ProjectDetector

The individual modules stay importable directly (``from core.setup import Setup``)
for code that wants a narrower dependency.
"""
from .console import _force_utf8_console
from .detector import (
    ExtensionType,
    ProjectDetector,
    QoLExtensionsRegistry,
)
from .executor import CommandExecutor
from .orchestrator import ProjectOrchestrator
from .security import SecurityScanner
from .setup import Setup
from .workspace import ProjectBootstrapper, WorkspaceDetector

# Windows consoles default to cp1252, which cannot encode the emoji used in
# the progress output. Fix the standard streams as soon as the package is
# imported, so every entry point gets it.
_force_utf8_console()

# The CLI pulls in tkinter, which is not available on every machine; keep it
# optional so the rest of the package stays importable in headless setups.
try:  # pragma: no cover - depends on the host having tkinter
    from .cli import InteractiveCLI, main
except ImportError:  # pragma: no cover
    InteractiveCLI = None  # type: ignore[assignment]

    def main(*args, **kwargs):  # type: ignore[misc]
        raise RuntimeError(
            "tkinter is not available on this machine, so the interactive CLI "
            "cannot start. Use the web dashboard instead: python app.py"
        )


__all__ = [
    "CommandExecutor",
    "ExtensionType",
    "InteractiveCLI",
    "ProjectBootstrapper",
    "ProjectDetector",
    "ProjectOrchestrator",
    "QoLExtensionsRegistry",
    "SecurityScanner",
    "Setup",
    "WorkspaceDetector",
    "main",
]
