"""API surface tests."""
import json

from fastapi.testclient import TestClient

from app import app


def _client():
    # base_url must be an allowed host: TrustedHostMiddleware rejects "testserver".
    return TestClient(app, base_url="http://127.0.0.1")


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


def test_config_endpoint():
    import app as webapp

    r = _client().get("/api/config", headers={"X-Setup-Token": webapp.API_TOKEN})
    assert r.status_code == 200
    assert "safe_root" in r.json()
