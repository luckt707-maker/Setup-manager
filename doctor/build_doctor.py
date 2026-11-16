"""Runs a full doctor pass and reports what it found.

A pass has three parts:

1. **Machine health** - the checks in :mod:`doctor.diagnostics`.
2. **Build diagnosis** - optionally run a build command and match its output
   against the knowledge base.
3. **Repairs** - the changes that are available for the findings above.

The result is written to ``reports/`` as JSON and appended to the history in
``logs/``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .diagnostics import (
    Diagnostic,
    diagnose_build_output,
    run_machine_diagnostics,
    summarize,
)
from .dependencies import check_project_dependencies
from .learning import record_learned_fix
from .log_manager import DoctorLogManager
from .repairs import Repair, apply_repair, available_repairs

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"


class BuildDoctor:
    """Coordinates diagnostics, build-failure analysis and repairs."""

    def __init__(
        self,
        project_path: Optional[Path] = None,
        log_manager: Optional[DoctorLogManager] = None,
        report_dir: Optional[Path] = None,
    ):
        self.project_path = Path(project_path) if project_path else None
        self.log_manager = log_manager or DoctorLogManager()
        self.report_dir = Path(report_dir) if report_dir else REPORT_DIR

    # ------------------------------------------------------------------ #
    def run(
        self,
        build_command: Optional[List[str]] = None,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """Run a full doctor pass and return the result as a dictionary."""
        diagnostics: List[Diagnostic] = run_machine_diagnostics(self.project_path)

        if self.project_path is not None:
            # The project's own dependencies and files are part of "is this
            # folder ready to run".
            diagnostics.extend(check_project_dependencies(self.project_path))

        build: Dict[str, Any] = {"ran": False}
        if build_command:
            build = self._run_build(build_command, timeout)
            diagnostics.extend(diagnose_build_output(build.get("output", "")))

        repairs = available_repairs(diagnostics, self.project_path)

        result: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "project": str(self.project_path) if self.project_path else None,
            "summary": summarize(diagnostics),
            "diagnostics": [d.to_dict() for d in diagnostics],
            "build": build,
            "repairs": [r.to_dict() for r in repairs],
        }

        self.log_manager.record(
            {
                "project": result["project"],
                "summary": result["summary"],
                "build_ok": build.get("returncode") == 0 if build.get("ran") else None,
            }
        )
        return result

    def _run_build(self, command: List[str], timeout: int) -> Dict[str, Any]:
        """Run a build command and capture its output."""
        cwd = str(self.project_path) if self.project_path else None
        try:
            proc = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            return {
                "ran": True,
                "command": command,
                "returncode": proc.returncode,
                "output": (proc.stdout or "") + (proc.stderr or ""),
            }
        except subprocess.TimeoutExpired:
            return {
                "ran": True,
                "command": command,
                "returncode": None,
                "output": f"The build timed out after {timeout} seconds.",
            }
        except FileNotFoundError:
            return {
                "ran": True,
                "command": command,
                "returncode": None,
                "output": f"Command not found: {command[0]}",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ran": True,
                "command": command,
                "returncode": None,
                "output": f"Could not run the build: {exc}",
            }

    # ------------------------------------------------------------------ #
    def write_report(self, result: Dict[str, Any]) -> Optional[Path]:
        """Write a result to the reports directory. Returns the file path."""
        try:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = self.report_dir / f"doctor-{stamp}.json"
            target.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
            return target
        except OSError:
            return None

    def _problems(self) -> Dict[str, Diagnostic]:
        """The findings that are a problem right now, by id."""
        found = run_machine_diagnostics(self.project_path)
        if self.project_path is not None:
            found.extend(check_project_dependencies(self.project_path))
        return {d.id: d for d in found if d.is_problem}

    def apply(self, repair: Repair, dry_run: bool = False, learn: bool = True) -> Dict[str, Any]:
        """Apply one repair and record the outcome.

        When the repair changed something and the problem it targets is gone
        afterwards, the fix is remembered in the knowledge base.
        """
        before = self._problems() if (learn and not dry_run) else {}
        changed, message = apply_repair(repair, dry_run=dry_run)
        verified = False
        learned = False
        target = before.get(repair.applies_to)
        if changed and target is not None:
            verified = repair.applies_to not in self._problems()
            if verified:
                learned = record_learned_fix(
                    target.title,
                    target.title,
                    target.detail or repair.description,
                    target.fix or repair.title,
                )
        self.log_manager.record({
            "action": "repair", "repair": repair.id, "changed": changed,
            "verified": verified, "learned": learned,
        })
        return {
            "repair": repair.id, "changed": changed, "message": message,
            "verified": verified, "learned": learned,
        }


# --------------------------------------------------------------------------- #
# Command line entry point
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    """``python -m doctor.build_doctor [folder] [--build "cmd args"]``"""
    args = list(argv if argv is not None else sys.argv[1:])
    folder: Optional[str] = None
    build: Optional[List[str]] = None

    while args:
        arg = args.pop(0)
        if arg == "--build" and args:
            from core.cmdline import split_command

            # Windows paths keep their backslashes this way; a POSIX split
            # would eat them and the build script would not be found.
            build = split_command(args.pop(0))
        elif not folder:
            folder = arg

    doctor = BuildDoctor(Path(folder) if folder else None)
    result = doctor.run(build_command=build)

    print("\n🩺 DOCTOR REPORT")
    print("-" * 50)
    for item in result["diagnostics"]:
        if item["status"] == "ok":
            continue
        marker = "❌" if item["status"] == "fail" else "⚠️"
        print(f"{marker} {item['title']}: {item['detail']}")
        if item.get("fix"):
            print(f"   → {item['fix']}")
    summary = result["summary"]
    print("-" * 50)
    print(f"ok: {summary['ok']}   warnings: {summary['warn']}   failures: {summary['fail']}")
    if result["repairs"]:
        print(f"Repairs available: {', '.join(r['id'] for r in result['repairs'])}")
    report = doctor.write_report(result)
    if report:
        print(f"Report written to {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
