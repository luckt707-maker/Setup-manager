"""Check a project for missing files and folders, and for package updates.

Nothing is downloaded unless --download is given. A download happens only
when the package is one the project already declares, the version still
fits that declaration, and the file is a checksummed wheel or npm tarball
from the official registry. Saved files are not installed.

  venv\\Scripts\\python.exe scripts\\check_project.py PATH
  venv\\Scripts\\python.exe scripts\\check_project.py PATH --download
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.project_check import check_project, download_safe_updates, format_report  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check missing files and safe package updates.")
    parser.add_argument("path", help="Project folder")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Save updates the check marks safe. Nothing is installed.",
    )
    args = parser.parse_args(argv)
    root = Path(args.path).expanduser()
    if not root.is_dir():
        print("Give a project folder.", file=sys.stderr)
        return 2
    report = download_safe_updates(root) if args.download else check_project(root)
    print(format_report(report))
    if not args.download:
        return 0
    failed = [
        item for item in report["downloads"]
        if not item.get("downloaded") and any(
            update.get("safe") and update.get("name") == item.get("name")
            for update in report["updates"]
        )
    ]
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    sys.exit(main())
