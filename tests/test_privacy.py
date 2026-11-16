"""This PC can be checked for known trackers. A person's name is not a target."""

from fastapi.testclient import TestClient

import app as webapp
from core.privacy import classify


H = {"X-Setup-Token": webapp.API_TOKEN}


def test_a_known_tracker_is_reported():
    report = classify(["mspy.exe"], [], [])
    finding = report["findings"][0]
    assert finding["kind"] == "tracker"
    assert finding["status"] == "warn"
    assert "mSpy" in finding["title"]
    assert "tracking program" in report["summary"]


def test_a_remote_tool_is_only_a_note():
    report = classify(["AnyDesk.exe"], ["TeamViewer.lnk"], [])
    kinds = {item["kind"] for item in report["findings"]}
    assert kinds == {"remote"}
    assert "did not install" in report["summary"]


def test_ordinary_programs_stay_quiet():
    report = classify(
        ["python.exe", "chrome.exe"],
        ["OneDrive"],
        [r"\Microsoft\Windows\UpdateOrchestrator\Schedule Scan"],
    )
    assert report["findings"] == []
    assert report["checklist"]


def test_the_privacy_check_does_not_take_a_person(monkeypatch):
    monkeypatch.setattr(webapp, "check_this_pc", lambda: classify(["notepad.exe"], [], []))
    client = TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    response = client.post("/privacy-check", headers=H)
    assert response.status_code == 200
    body = response.json()
    assert body["findings"] == []
    assert "phone" in body["checklist"][0].lower()


def test_windows_tools_are_called_by_their_full_path(monkeypatch):
    import core.privacy as privacy

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    for name in ("tasklist", "reg", "schtasks"):
        path = privacy.system_tool(name).replace("/", "\\")
        assert path.lower() == rf"c:\windows\system32\{name}.exe"
    seen = []
    # Pretend to be Windows inside privacy.py only; pathlib must keep its real os.
    import os
    import types
    monkeypatch.setattr(privacy, "os", types.SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(privacy, "_run", lambda command, timeout=20: seen.append(command[0]) or "")
    privacy._process_names()
    privacy._startup_names()
    privacy._task_names()
    assert len(seen) == 3
    assert all("System32" in tool and tool.endswith(".exe") for tool in seen)


def test_a_failed_check_does_not_show_the_raw_error(monkeypatch):
    def broken():
        raise RuntimeError(r"secret detail C:\Users\someone\private")

    monkeypatch.setattr(webapp, "check_this_pc", broken)
    client = TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    response = client.post("/privacy-check", headers=H)
    assert response.status_code == 500
    assert "secret detail" not in response.text
    assert "could not finish" in response.json()["detail"]
