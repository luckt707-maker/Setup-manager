"""Command line options for starting the dashboard.

The Desktop shortcut starts the app through ``pythonw.exe``, which cannot
rely on an environment block, so the things it needs - open the browser,
pin the port, pin the projects folder - are passed as arguments instead.

Standard library only, like the rest of ``core``.
"""
import argparse
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class LaunchOptions:
    """What the command line asked for."""

    port: Optional[int] = None
    auto_open: bool = False
    safe_root: Optional[str] = None
    headless: bool = False


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="setup-manager",
        description="Start the Setup Manager dashboard.",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Serve on this port instead of the first free one.",
    )
    parser.add_argument(
        "--auto-open",
        action="store_true",
        help="Open the dashboard in the browser once it is listening.",
    )
    parser.add_argument(
        "--safe-root",
        help="The only folder the dashboard is allowed to work in.",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Do not show the launcher window.",
    )
    return parser


def parse_launch_args(argv: Sequence[str]) -> LaunchOptions:
    """Read the launch options. Unknown arguments are an error."""
    args = build_parser().parse_args(list(argv))
    return LaunchOptions(
        port=args.port,
        auto_open=args.auto_open,
        safe_root=args.safe_root,
        headless=args.no_gui,
    )
