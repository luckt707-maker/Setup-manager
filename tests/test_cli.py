"""Tests for the interactive CLI's web dashboard launcher.

The dashboard may not end up on the preferred port, so the launcher has
to find where it actually is rather than assuming.
"""
import pytest

pytest.importorskip(
    "tkinter",
    reason="The terminal menu needs tkinter, which this Python does not include.",
)

from core import cli
from core.server_state import write_state


@pytest.fixture(autouse=True)
def isolated_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_STATE_FILE", str(tmp_path / "server.json"))
    # Keep the port scan away from any dashboard that happens to be running
    # on this machine.
    monkeypatch.setenv("SETUP_MANAGER_PORT_RANGE", "40000-40010")


@pytest.fixture
def opened_urls(monkeypatch):
    """Capture the URLs the launcher opens, instead of opening a browser."""
    urls = []
    monkeypatch.setattr("webbrowser.open", urls.append)
    return urls


def test_launch_web_interface_reuses_a_running_dashboard(
    tmp_path, monkeypatch, opened_urls, fake_setup_manager
):
    write_state(fake_setup_manager)
    monkeypatch.setattr("subprocess.Popen", _fail_if_called)

    assert cli.InteractiveCLI().launch_web_interface() is True
    assert opened_urls == [f"http://127.0.0.1:{fake_setup_manager}"]


def test_launch_web_interface_opens_the_port_the_server_actually_chose(
    tmp_path, monkeypatch, opened_urls, fake_setup_manager
):
    """The server picks its own port and records it; the launcher must read it."""
    monkeypatch.setattr("time.sleep", lambda *_: None)

    def fake_popen(*args, **kwargs):
        # The server started and recorded where it is listening.
        write_state(fake_setup_manager)
        return None

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    assert cli.InteractiveCLI().launch_web_interface() is True
    assert opened_urls == [f"http://127.0.0.1:{fake_setup_manager}"]


def test_launch_web_interface_reports_a_server_that_never_came_up(
    tmp_path, monkeypatch, opened_urls
):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: None)

    assert cli.InteractiveCLI().launch_web_interface() is False
    assert opened_urls == []


def _fail_if_called(*args, **kwargs):
    raise AssertionError("the launcher should not have started a second server")
