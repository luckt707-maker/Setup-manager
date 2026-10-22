#!/usr/bin/env python3
"""Backwards-compatible entry point for the Setup Manager engine.

The engine itself now lives in the ``core`` package; this file keeps the
old ``python setup_manager.py`` invocation working and re-exports the public
API for anything that still does ``import setup_manager``.
"""
from core import (  # noqa: F401  (re-exported on purpose)
    CommandExecutor,
    ExtensionType,
    InteractiveCLI,
    ProjectBootstrapper,
    ProjectDetector,
    ProjectOrchestrator,
    QoLExtensionsRegistry,
    SecurityScanner,
    Setup,
    WorkspaceDetector,
    main,
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

if __name__ == "__main__":
    main()
