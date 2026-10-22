"""Launch argument tests.

The desktop shortcut needs to start the dashboard with the browser open
and the projects folder pinned, without an environment block it cannot
rely on. These arguments are that contract.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.launch_args import parse_launch_args


def test_no_arguments_change_nothing():
    options = parse_launch_args([])

    assert options.port is None
    assert options.auto_open is False
    assert options.safe_root is None
    assert options.headless is False


def test_auto_open_is_requested_explicitly():
    options = parse_launch_args(["--auto-open"])

    assert options.auto_open is True


def test_the_port_can_be_pinned():
    options = parse_launch_args(["--port", "8015"])

    assert options.port == 8015


def test_a_port_that_is_not_a_number_is_refused():
    import pytest

    with pytest.raises(SystemExit):
        parse_launch_args(["--port", "eight-thousand"])


def test_the_projects_root_can_be_pinned():
    options = parse_launch_args(["--safe-root", r"D:\projects"])

    assert options.safe_root == r"D:\projects"


def test_headless_means_no_window():
    options = parse_launch_args(["--no-gui"])

    assert options.headless is True


def test_unknown_arguments_are_refused():
    import pytest

    with pytest.raises(SystemExit):
        parse_launch_args(["--make-me-a-sandwich"])
