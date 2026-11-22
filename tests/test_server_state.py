"""Tests for port selection and server discovery.

These cover the helpers in ``core.server_state``: picking a free port,
recording where the server is, and finding a Setup Manager that is
already running.
"""
import os
import socket
from pathlib import Path

import pytest

from core import server_state


def _a_free_port() -> int:
    """A port number that is free right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def isolated_state_file(tmp_path, monkeypatch):
    """Keep the tests away from the real runtime state file."""
    monkeypatch.setenv("SETUP_MANAGER_STATE_FILE", str(tmp_path / "server.json"))


@pytest.fixture
def live_server(fake_setup_manager):
    """A running HTTP server on a free port, pretending to be Setup Manager."""
    return fake_setup_manager


@pytest.fixture
def a_busy_port(busy_port):
    """A port that is occupied by something that is not Setup Manager."""
    return busy_port


# --------------------------------------------------------------------------- #
#  Port selection
# --------------------------------------------------------------------------- #

def test_find_free_port_returns_the_start_of_the_range_when_it_is_free():
    start = _a_free_port()
    assert server_state.find_free_port(start, start + 20) == start


def test_find_free_port_skips_a_port_that_is_taken(a_busy_port):
    chosen = server_state.find_free_port(a_busy_port, a_busy_port + 20)
    assert chosen > a_busy_port


def test_find_free_port_raises_when_every_port_in_the_range_is_taken(a_busy_port):
    with pytest.raises(RuntimeError):
        server_state.find_free_port(a_busy_port, a_busy_port)


# --------------------------------------------------------------------------- #
#  The runtime state file
# --------------------------------------------------------------------------- #

def test_write_state_records_the_port_and_pid(tmp_path):
    path = tmp_path / "server.json"
    server_state.write_state(8123, pid=4242, path=path)
    state = server_state.read_state(path=path)
    assert state["port"] == 8123
    assert state["pid"] == 4242
    assert state["url"] == "http://127.0.0.1:8123"


def test_read_state_returns_none_when_there_is_no_state_file(tmp_path):
    assert server_state.read_state(path=tmp_path / "missing.json") is None


def test_clear_state_removes_the_state_file(tmp_path):
    path = tmp_path / "server.json"
    server_state.write_state(8123, path=path)
    server_state.clear_state(path=path)
    assert not path.exists()
    assert server_state.read_state(path=path) is None


def test_state_path_follows_the_environment_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_STATE_FILE", str(tmp_path / "elsewhere.json"))
    assert server_state.state_path() == tmp_path / "elsewhere.json"


def test_state_path_defaults_to_the_logs_folder(monkeypatch):
    monkeypatch.delenv("SETUP_MANAGER_STATE_FILE", raising=False)
    assert server_state.state_path().name == "server.json"
    assert server_state.state_path().parent.name == "logs"


# --------------------------------------------------------------------------- #
#  The port range
# --------------------------------------------------------------------------- #

def test_port_range_defaults_to_the_standard_range(monkeypatch):
    monkeypatch.delenv("SETUP_MANAGER_PORT_RANGE", raising=False)
    assert server_state.port_range() == (server_state.DEFAULT_PORT, server_state.PORT_RANGE_END)


def test_port_range_can_be_set_in_the_environment(monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_PORT_RANGE", "9000-9010")
    assert server_state.port_range() == (9000, 9010)


def test_port_range_ignores_a_nonsense_value(monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_PORT_RANGE", "not-a-range")
    assert server_state.port_range() == (server_state.DEFAULT_PORT, server_state.PORT_RANGE_END)


# --------------------------------------------------------------------------- #
#  Finding a server that is already running
# --------------------------------------------------------------------------- #

def test_find_running_instance_finds_a_live_server_from_the_state_file(tmp_path, live_server):
    path = tmp_path / "server.json"
    server_state.write_state(live_server, path=path)
    found = server_state.find_running_instance(path=path, scan_from=40000, scan_to=40100)
    assert found is not None
    assert found["port"] == live_server
    assert found["url"] == f"http://127.0.0.1:{live_server}"


def test_find_running_instance_finds_a_live_server_without_a_state_file(live_server):
    found = server_state.find_running_instance(
        path=Path(os.devnull) / "nonexistent.json",
        scan_from=live_server,
        scan_to=live_server,
    )
    assert found is not None
    assert found["port"] == live_server


def test_find_running_instance_ignores_a_stale_state_file(tmp_path):
    path = tmp_path / "server.json"
    server_state.write_state(_a_free_port(), pid=999999, path=path)
    found = server_state.find_running_instance(path=path, scan_from=40000, scan_to=40100)
    assert found is None


def test_find_running_instance_ignores_a_foreign_server_on_the_port(tmp_path, a_busy_port):
    path = tmp_path / "server.json"
    server_state.write_state(a_busy_port, path=path)
    found = server_state.find_running_instance(path=path, scan_from=40000, scan_to=40100)
    assert found is None


# --------------------------------------------------------------------------- #
#  Choosing which port to serve on
# --------------------------------------------------------------------------- #

def test_choose_port_uses_a_pinned_port_when_it_is_free():
    port = _a_free_port()
    chosen, running = server_state.choose_port(explicit=port, scan_from=40000, scan_to=40100)
    assert chosen == port
    assert running is None


def test_choose_port_reuses_a_running_instance_instead_of_starting_a_second(live_server):
    chosen, running = server_state.choose_port(
        explicit=None, scan_from=live_server, scan_to=live_server
    )
    assert running == f"http://127.0.0.1:{live_server}"


def test_choose_port_reuses_a_running_instance_on_the_pinned_port(live_server):
    chosen, running = server_state.choose_port(
        explicit=live_server, scan_from=40000, scan_to=40100
    )
    assert chosen == live_server
    assert running == f"http://127.0.0.1:{live_server}"


def test_choose_port_refuses_a_number_that_is_not_a_port():
    """0 and 70000 are not ports the dashboard can listen on."""
    for number in (0, -1, 70000):
        with pytest.raises(RuntimeError, match="not a usable port"):
            server_state.choose_port(explicit=number, scan_from=40000, scan_to=40100)


def test_a_blank_or_nonsense_port_setting_is_explained():
    """An empty value means 'pick one'. Words are a mistake to explain."""
    assert server_state.read_port_setting(None) is None
    assert server_state.read_port_setting("  ") is None
    assert server_state.read_port_setting("8010") == 8010
    with pytest.raises(ValueError, match="not a port number"):
        server_state.read_port_setting("abc")
    with pytest.raises(ValueError, match="not a usable port"):
        server_state.read_port_setting("0")
    with pytest.raises(ValueError, match="not a usable port"):
        server_state.read_port_setting("70000")


def test_choose_port_refuses_a_pinned_port_taken_by_something_else(a_busy_port):
    with pytest.raises(RuntimeError):
        server_state.choose_port(explicit=a_busy_port, scan_from=40000, scan_to=40100)


def test_choose_port_moves_up_when_the_start_of_the_range_is_taken(a_busy_port):
    chosen, running = server_state.choose_port(
        explicit=None, scan_from=a_busy_port, scan_to=a_busy_port + 20
    )
    assert chosen > a_busy_port
    assert running is None
