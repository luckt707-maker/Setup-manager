"""The name search used when the folder is the unknown part."""

from pathlib import Path

from fastapi.testclient import TestClient

import app as webapp
from conftest import SAFE_ROOT
from core.where import describe_folder, find_programs


H = {"X-Setup-Token": webapp.API_TOKEN}


def _client():
    return TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def test_a_name_search_tells_source_from_an_environment(tmp_path):
    programs = tmp_path / "Programs"
    programs.mkdir()
    phone = programs / "PhoneInfoga"
    phone.mkdir()
    (phone / "go.mod").write_text("module example.com/phone\n\ngo 1.20\n", encoding="utf-8")
    (phone / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    (phone / "phoneinfoga.exe").write_bytes(b"")

    installed = programs / "Sherlock"
    (installed / "venv" / "Scripts").mkdir(parents=True)
    (installed / ".venv").mkdir()
    (installed / "venv" / "Scripts" / "sherlock.exe").write_bytes(b"")

    source = tmp_path / "dev" / "sherlock"
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname='sherlock'\n", encoding="utf-8")

    phone_hits = find_programs([programs, tmp_path / "dev"], "phone infoga")
    assert [item["path"] for item in phone_hits] == [str(phone)]
    assert "Go" in phone_hits[0]["note"]
    assert "already" in phone_hits[0]["note"]

    hits = find_programs([programs, tmp_path / "dev"], "sherlock")
    notes = {Path(item["path"]).name: item["note"] for item in hits}
    assert "environment" in notes["Sherlock"]
    assert "sherlock" in notes["Sherlock"]
    assert "Python" in notes["sherlock"]
    assert "source code is a different folder" in notes["Sherlock"]


def test_a_folder_with_only_a_python_file_is_still_a_program(tmp_path):
    folder = tmp_path / "sherlock-complete"
    folder.mkdir()
    (folder / "main.py").write_text("print('hi')\n", encoding="utf-8")
    assert "small Python folder" in describe_folder(folder)


def test_the_dashboard_folder_is_named_as_this_page(tmp_path):
    folder = tmp_path / "SetupManagerv2"
    folder.mkdir()
    (folder / "app.py").write_text("print('dashboard')\n", encoding="utf-8")
    assert "page you are using" in describe_folder(folder)


def test_where_endpoint_searches_the_projects_folder():
    project = SAFE_ROOT / "PhoneInfoga-where"
    project.mkdir(exist_ok=True)
    (project / "go.mod").write_text("module example.com/phone\n\ngo 1.20\n", encoding="utf-8")
    response = _client().get("/where", params={"q": "phoneinfoga-where"}, headers=H)
    assert response.status_code == 200
    body = response.json()
    assert any(item["path"] == str(project) for item in body["matches"])
    assert body["roots"]
