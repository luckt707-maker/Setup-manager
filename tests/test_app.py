"""API surface tests."""
import json

from fastapi.testclient import TestClient

from app import app


def _client():
    # base_url must be an allowed host: TrustedHostMiddleware rejects "testserver".
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def test_health():
    r = _client().get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_health_identifies_the_app():
    """Other programs listen on ports too; /health says who is who."""
    r = _client().get("/health")
    assert r.json()["app"] == "setup-manager"


def test_startup_records_where_the_server_is(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_STATE_FILE", str(tmp_path / "server.json"))
    import app as webapp

    monkeypatch.setattr(webapp, "APP_PORT", 8123)
    with _client():
        state = json.loads((tmp_path / "server.json").read_text(encoding="utf-8"))
    assert state["port"] == 8123
    assert state["url"] == "http://127.0.0.1:8123"


def test_shutdown_forgets_where_the_server_was(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_STATE_FILE", str(tmp_path / "server.json"))
    import app as webapp

    monkeypatch.setattr(webapp, "APP_PORT", 8123)
    with _client():
        pass
    assert not (tmp_path / "server.json").exists()


def test_index_serves_the_ui():
    r = _client().get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_static_assets_are_served():
    c = _client()
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/static/index.html").status_code == 200


def test_system_doctor_shape():
    """The dashboard reads r.tools[...]['status'], so the shape is a contract."""
    import app as webapp

    c = _client()
    r = c.get("/system/doctor", headers={"X-Setup-Token": webapp.API_TOKEN})
    assert r.status_code == 200
    tools = r.json()["tools"]
    assert "python" in tools
    assert set(tools["python"]) >= {"status", "path", "url"}
    # No project is selected, so an absent tool is not reported as an error.
    for info in tools.values():
        assert info["status"] in {"ready", "not_needed"}


def test_system_doctor_ignores_tools_the_project_does_not_need():
    import app as webapp

    project = _project_in_safe_root("doctor-python", {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "Dockerfile": "FROM python:3.12\n",
        "go.mod": "module myproject\n\ngo 1.21\n",
        "package.json": '{"dependencies":{"left-pad":"1.0.0"}}\n',
    })
    response = _client().get(
        "/system/doctor",
        params={"path": str(project)},
        headers={"X-Setup-Token": webapp.API_TOKEN},
    )
    assert response.status_code == 200
    tools = response.json()["tools"]
    assert tools["python"]["needed"] is True
    for name in ("node", "npm", "docker", "make"):
        assert tools[name]["needed"] is False
        assert tools[name]["status"] != "missing"
    assert "go" not in tools


def test_config_endpoint():
    import app as webapp

    r = _client().get("/api/config", headers={"X-Setup-Token": webapp.API_TOKEN})
    assert r.status_code == 200
    assert "safe_root" in r.json()


def _project_in_safe_root(name, files):
    """A project folder inside the safe root the tests use."""
    from conftest import SAFE_ROOT

    project = SAFE_ROOT / name
    project.mkdir(parents=True, exist_ok=True)
    for filename, text in files.items():
        path = project / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


def test_doctor_dependencies_says_what_the_folder_is_missing():
    """The tool the user asked for: what a project is missing."""
    import app as webapp

    project = _project_in_safe_root("missing-deps", {"requirements.txt": "requests\n"})

    r = _client().post(
        "/doctor/dependencies",
        json={"path": str(project)},
        headers={"X-Setup-Token": webapp.API_TOKEN},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    ids = [finding["id"] for finding in body["findings"]]
    assert "dependencies.not_installed" in ids, body
    detail = " ".join(finding["detail"] for finding in body["findings"])
    assert "requests" in detail, body


def test_doctor_dependencies_needs_the_token():
    import app as webapp

    project = _project_in_safe_root("missing-deps-unauthenticated", {"requirements.txt": "requests\n"})

    r = _client().post("/doctor/dependencies", json={"path": str(project)})

    assert r.status_code == 401, r.text


def test_doctor_dependencies_refuses_a_path_outside_the_safe_root():
    import app as webapp

    r = _client().post(
        "/doctor/dependencies",
        json={"path": r"C:\Windows"},
        headers={"X-Setup-Token": webapp.API_TOKEN},
    )

    assert r.status_code == 400, r.text


def test_install_missing_packages_starts_a_task():
    """The button next to the report: put the missing packages in."""
    import app as webapp

    project = _project_in_safe_root("install-missing", {"requirements.txt": "requests\n"})

    r = _client().post(
        "/task/install-missing",
        json={"path": str(project)},
        headers={"X-Setup-Token": webapp.API_TOKEN},
    )

    assert r.status_code == 200, r.text
    assert r.json()["task_id"], r.text


def test_install_missing_packages_refuses_a_path_outside_the_safe_root():
    import app as webapp

    r = _client().post(
        "/task/install-missing",
        json={"path": r"C:\Windows"},
        headers={"X-Setup-Token": webapp.API_TOKEN},
    )

    assert r.status_code == 400, r.text


def test_launching_a_program_that_is_not_installed_says_so(tmp_path, monkeypatch):
    """The button names a program; if it is not there, the user is told."""
    import shutil

    import app as webapp

    monkeypatch.setattr(shutil, "which", lambda name: None)
    project = _project_in_safe_root("launch-missing-program", {"main.py": "print('hi')\n"})

    r = _client().post(
        "/task/launch",
        json={"path": str(project), "candidate_index": 0, "run_mode": "host"},
        headers={"X-Setup-Token": webapp.API_TOKEN},
    )

    assert r.status_code == 400, r.text
    assert "not installed" in r.json()["detail"], r.text
