"""Print folders that match a program name.

Run from Setup Manager:

    venv\\Scripts\\python.exe scripts\\where.py sherlock

The script looks in the same folders the dashboard is allowed to open.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.settings import allowed_project_roots  # noqa: E402
from core.where import find_programs  # noqa: E402


def main(argv: list[str]) -> int:
    roots = allowed_project_roots()
    print("Looking in:")
    for root in roots:
        print(f"  {root}")
    if len(argv) < 2 or not argv[1].strip():
        print("Type a name after the command, for example: where.py phoneinfoga")
        return 0
    matches = find_programs(roots, argv[1])
    if not matches:
        print(f"No folder named like {argv[1].strip()!r} in those places.")
        return 1
    print()
    for item in matches:
        print(item["name"])
        print(f"  {item['path']}")
        print(f"  {item['note']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
