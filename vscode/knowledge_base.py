"""Known VS Code problems and how to put them right.

This is the VS Code counterpart of :mod:`doctor.knowledge_base`: a small table
of the failures people actually hit when Setup Manager drives VS Code - a
missing interpreter, a terminal that will not start, an extension that failed
to load - together with the cause and the fix.
"""
from __future__ import annotations

from typing import Dict, Iterator, Optional, Tuple


class VSCodeKnowledgeBase:
    """Matches VS Code error text to a cause and a fix."""

    KNOWN_ISSUES: Dict[str, Dict[str, str]] = {
        "python.interpreter.notFound": {
            "name": "Python interpreter not found",
            "cause": "VS Code is set to an interpreter that is not installed on this machine.",
            "fix": "Pick an installed interpreter: Ctrl+Shift+P > Python: Select Interpreter",
        },
        "Cannot find module": {
            "name": "Module missing in the VS Code terminal",
            "cause": "The terminal is using the global interpreter instead of the project venv.",
            "fix": "Activate the project venv, or set python.defaultInterpreterPath in settings.json",
        },
        "No module named": {
            "name": "Package not installed in the selected environment",
            "cause": "The extension and the terminal disagree about which Python is in use.",
            "fix": "Install into the venv: <venv>\\Scripts\\python.exe -m pip install <package>",
        },
        "EPERM": {
            "name": "File is locked by another process",
            "cause": "VS Code still has the file open, or an antivirus scanner is holding it.",
            "fix": "Close the file in VS Code, then retry the install",
        },
        "spawn ENOENT": {
            "name": "VS Code cannot start the configured command",
            "cause": "The path in the task or terminal profile does not exist.",
            "fix": "Correct the path in .vscode/tasks.json or settings.json",
        },
        "terminal.integrated.profiles": {
            "name": "Terminal profile not found",
            "cause": "settings.json points at a shell that is not installed.",
            "fix": "Set terminal.integrated.defaultProfile.windows to an installed shell",
        },
        "Extension host terminated": {
            "name": "VS Code extension host crashed",
            "cause": "An extension faulted, often after a settings change.",
            "fix": "Run 'Developer: Reload Window', and disable extensions one at a time to find the culprit",
        },
        "Could not resolve": {
            "name": "npm dependency could not be resolved",
            "cause": "A package version in package.json does not exist in the registry.",
            "fix": "Check the version with: npm view <package> versions",
        },
    }

    @classmethod
    def all_issues(cls) -> Dict[str, Dict[str, str]]:
        """The whole table, for callers that want to list it."""
        return dict(cls.KNOWN_ISSUES)

    @classmethod
    def iter_matches(cls, text: str) -> Iterator[Tuple[str, Dict[str, str]]]:
        """Yield (signature, issue) for every known issue present in text."""
        if not text:
            return
        lowered = text.lower()
        for signature in sorted(cls.KNOWN_ISSUES, key=len, reverse=True):
            if signature.lower() in lowered:
                yield signature, cls.KNOWN_ISSUES[signature]

    @classmethod
    def lookup(cls, text: str) -> Optional[Dict[str, str]]:
        """Return the first known issue found in text, or ``None``."""
        for _signature, issue in cls.iter_matches(text):
            return issue
        return None

    @classmethod
    def print_match(cls, text: str) -> None:
        """Print the matching issue, if there is one."""
        issue = cls.lookup(text)
        if not issue:
            print("No known VS Code issue matched.")
            return
        print()
        print("=" * 40)
        print("KNOWN VS CODE ISSUE DETECTED")
        print("=" * 40)
        print(f"Name : {issue['name']}")
        print(f"Cause: {issue['cause']}")
        print(f"Fix  : {issue['fix']}")
        print("=" * 40)
