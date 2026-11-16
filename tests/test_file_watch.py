"""A sneaky file or program is reported only after Setup Manager is open."""

from pathlib import Path

from fastapi.testclient import TestClient

import app as webapp
import threading
import time

import pytest

import core.file_watch as fw
from core.file_watch import FileWatch, WatchDisabled, WatchRunner

H = {"X-Setup-Token": webapp.API_TOKEN}
DOWNLOADS = Path(r"C:\Users\Someone\Downloads")
DESKTOP = Path(r"C:\Users\Someone\Desktop")
STARTUP = Path(r"C:\Users\Someone\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup")


def _watch() -> FileWatch:
    watch = FileWatch()
    watch.note([], [], [DOWNLOADS, DESKTOP, STARTUP])
    return watch


def test_a_file_already_there_is_listed_and_a_new_one_is_separate():
    watch = FileWatch()
    first = watch.note([DOWNLOADS / "old-setup.exe"], [], [DOWNLOADS])
    assert len(first) == 1
    assert first[0]["when"] == "already"
    second = watch.note(
        [DOWNLOADS / "old-setup.exe", DOWNLOADS / "invoice.pdf.exe"],
        [],
        [DOWNLOADS],
    )
    assert len(second) == 1
    assert second[0]["when"] == "new"
    assert "looks like a document" in second[0]["detail"]
    report = watch.snapshot()
    assert "new" in report["summary"].lower()


def test_a_versioned_desktop_shortcut_stays_quiet():
    watch = _watch()
    found = watch.note([DESKTOP / "Creality Print 7.3.lnk"], [], [DESKTOP])
    assert found == []


def test_a_plain_document_and_a_desktop_shortcut_stay_quiet():
    watch = _watch()
    found = watch.note(
        [DOWNLOADS / "notes.txt", DESKTOP / "Editor.lnk"],
        [],
        [DOWNLOADS, DESKTOP],
    )
    assert found == []


def test_a_program_that_starts_from_downloads_is_reported():
    watch = _watch()
    chrome = {
        "pid": "10",
        "name": "chrome.exe",
        "executable": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "command": "",
    }
    quiet = watch.note([], [chrome], [DOWNLOADS])
    assert quiet == []
    started = watch.note(
        [],
        [chrome, {
            "pid": "11",
            "name": "invoice.exe",
            "executable": str(DOWNLOADS / "invoice.exe"),
            "command": "",
        }],
        [DOWNLOADS],
    )
    assert len(started) == 1
    assert started[0]["kind"] == "program"
    assert "Downloads" in started[0]["detail"]


def test_a_script_host_that_opens_a_download_is_reported():
    watch = _watch()
    found = watch.note(
        [],
        [{
            "pid": "12",
            "name": "wscript.exe",
            "executable": r"C:\Windows\System32\wscript.exe",
            "command": r'"C:\Windows\System32\wscript.exe" "C:\Users\Someone\Downloads\invoice.js"',
        }],
        [DOWNLOADS],
    )
    assert len(found) == 1
    assert found[0]["title"] == "invoice.js"
    assert "wscript" not in found[0]["detail"].lower()


def test_a_new_startup_file_is_reported():
    watch = _watch()
    found = watch.note([STARTUP / "helper.lnk"], [], [STARTUP])
    assert len(found) == 1
    assert "Startup" in found[0]["detail"]


def test_the_page_can_read_the_watch_without_a_person(monkeypatch):
    def fake_poll(watch):
        return watch.note(
            [],
            [{
                "pid": "4",
                "name": "invoice.exe",
                "executable": str(DOWNLOADS / "invoice.exe"),
                "command": "",
            }],
            [DOWNLOADS],
        )

    monkeypatch.setenv("SETUP_MANAGER_FILE_WATCH", "1")
    local = WatchRunner(poll=fake_poll, interval=0.01)
    monkeypatch.setattr(webapp, "file_watch_runner", local)
    client = TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    assert client.post("/file-watch/start", headers=H).status_code == 200
    try:
        _until(lambda: local.status()["alerts"])
        response = client.get("/file-watch?name=someone", headers=H)
        assert response.status_code == 200
        body = response.json()
        assert body["running"] is True
        assert body["alerts"][0]["kind"] == "program"
        assert "someone" not in response.text
    finally:
        local.stop()
    page = client.get("/")
    assert 'id="fileWatchAlerts"' in page.text


def test_powershell_is_called_by_its_full_path(monkeypatch):
    import core.file_watch as fw

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    path = fw._powershell().replace("/", "\\").lower()
    assert path == r"c:\windows\system32\windowspowershell\v1.0\powershell.exe"


def test_the_watch_needs_the_token_and_this_machine():
    no_token = TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    assert no_token.get("/file-watch").status_code == 401
    elsewhere = TestClient(webapp.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
    assert elsewhere.get("/file-watch", headers=H).status_code == 403
    other_site = TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    assert other_site.get("/file-watch", headers={**H, "Origin": "http://evil.example"}).status_code == 403
    assert other_site.get("/file-watch", headers={**H, "Host": "evil.example"}).status_code == 400


# --------------------------------------------------------------------------- #
#  Off by default, start / stop, and the automatic stops
# --------------------------------------------------------------------------- #
def _until(check, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    raise AssertionError("timed out")


def _watch_threads():
    return [t for t in threading.enumerate() if t.name == "file-watch" and t.is_alive()]


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _counting_runner(**kwargs):
    calls = []

    def poll(watch):
        calls.append(time.monotonic())
        return []

    return WatchRunner(poll=poll, interval=0.01, **kwargs), calls


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_FILE_WATCH", "1")
    monkeypatch.delenv("SETUP_MANAGER_FILE_WATCH_MAX_MINUTES", raising=False)


def test_the_watch_is_off_at_import_and_at_startup(enabled, monkeypatch):
    looked = []
    monkeypatch.setattr(fw, "_list_processes", lambda: looked.append("ps") or [])
    monkeypatch.setattr(fw, "_list_files", lambda folders: looked.append("files") or [])
    assert webapp.file_watch_runner.running is False
    with TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        time.sleep(0.2)
        assert _watch_threads() == []
        body = client.get("/file-watch", headers=H).json()
        assert body["running"] is False
        assert body["started_at"] is None
        assert "no CPU" in body["summary"]
    assert looked == [], "nothing may look at the PC until the user starts the watch"


def test_start_is_idempotent_and_stop_ends_the_thread_at_once(enabled):
    runner, calls = _counting_runner()
    first = runner.start()
    second = runner.start()
    assert first["running"] and second["running"]
    assert first["started_at"] == second["started_at"]
    assert len(_watch_threads()) == 1
    _until(lambda: len(calls) >= 2)
    began = time.monotonic()
    stopped = runner.stop()
    runner.join(2)
    assert time.monotonic() - began < 1
    assert stopped["running"] is False and stopped["auto_stopped"] is False
    assert _watch_threads() == []
    count = len(calls)
    time.sleep(0.1)
    assert len(calls) == count, "no polling after stop"


def test_stop_does_not_wait_for_the_sleep_between_looks(enabled):
    runner = WatchRunner(poll=lambda watch: [], interval=30)
    runner.start()
    time.sleep(0.05)
    began = time.monotonic()
    runner.stop()
    runner.join(2)
    assert time.monotonic() - began < 1
    assert _watch_threads() == []


def test_the_watch_stops_when_the_page_stops_checking(enabled):
    clock = _Clock()
    runner = WatchRunner(poll=lambda watch: [], clock=clock, interval=0.01, idle_seconds=60)
    runner.start()
    clock.now += 30
    runner.status()  # a page poll is the heartbeat
    clock.now += 45
    time.sleep(0.1)
    assert runner.running, "a heartbeat 45 seconds ago keeps it running"
    clock.now += 20
    _until(lambda: not runner.running)
    runner.join(2)
    body = runner.status()
    assert body["auto_stopped"] is True
    assert "closed or hidden" in body["stopped_reason"]
    assert "Stopped automatically" in body["summary"]
    assert body["alerts"][-1]["kind"] == "note"
    assert "stopped automatically" in body["alerts"][-1]["title"]


def test_the_watch_stops_after_its_time_limit(enabled, monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_FILE_WATCH_MAX_MINUTES", "5")
    clock = _Clock()
    runner = WatchRunner(poll=lambda watch: [], clock=clock, interval=0.01, idle_seconds=60)
    runner.start()
    for _ in range(10):  # the page keeps checking, so only the limit can stop it
        clock.now += 29
        runner.status()
        time.sleep(0.02)
    assert runner.running
    clock.now += 20
    runner.status()
    _until(lambda: not runner.running)
    body = runner.status()
    assert body["auto_stopped"] is True
    assert "limit of 5 minutes" in body["stopped_reason"]
    assert any(item["kind"] == "note" for item in body["alerts"])


def test_the_default_time_limit_is_two_hours(enabled):
    assert fw.max_run_seconds() == 2 * 60 * 60


def test_the_disable_setting_refuses_start(monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_FILE_WATCH", "0")
    runner, calls = _counting_runner()
    with pytest.raises(WatchDisabled):
        runner.start()
    monkeypatch.setattr(webapp, "file_watch_runner", runner)
    client = TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    response = client.post("/file-watch/start", headers=H)
    assert response.status_code == 409
    assert "SETUP_MANAGER_FILE_WATCH=0" in response.json()["detail"]
    assert client.get("/file-watch", headers=H).json()["disabled"] is True
    assert runner.running is False and calls == []


def test_start_and_stop_are_guarded(enabled, monkeypatch):
    runner, calls = _counting_runner()
    monkeypatch.setattr(webapp, "file_watch_runner", runner)
    local = TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    elsewhere = TestClient(webapp.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
    for path in ("/file-watch/start", "/file-watch/stop"):
        assert local.post(path).status_code == 401
        assert local.post(path, headers={"X-Setup-Token": "wrong"}).status_code == 401
        assert elsewhere.post(path, headers=H).status_code == 403
        assert local.post(path, headers={**H, "Origin": "http://evil.example"}).status_code == 403
        assert local.post(path, headers={**H, "Host": "evil.example"}).status_code == 400
        assert local.get(path, headers=H).status_code == 405
    assert runner.running is False and calls == []
    assert local.post("/file-watch/start", headers=H).json()["running"] is True
    assert local.post("/file-watch/stop", headers=H).json()["running"] is False
    runner.join(2)


def test_closing_the_app_stops_the_watch(enabled, monkeypatch):
    runner, _ = _counting_runner()
    monkeypatch.setattr(webapp, "file_watch_runner", runner)
    with TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.post("/file-watch/start", headers=H).json()["running"] is True
    assert runner.running is False
    runner.join(2)
    assert _watch_threads() == []
