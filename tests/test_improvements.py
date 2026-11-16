"""Tests for the offline-AI, learning doctor, folder picking and dry-run changes."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as webapp
import ollama_client
from conftest import SAFE_ROOT
from core.settings import settings_manager
from core.tooling import make_command
from ollama_client import OllamaClient, pick_model_for_ram


def _client():
    return TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


H = {"X-Setup-Token": webapp.API_TOKEN}


# --------------------------------------------------------------------------- #
#  AI health check
# --------------------------------------------------------------------------- #
def _fake_ollama(models):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = {"version": "0.9"} if self.path == "/api/version" else {
                "models": [{"name": m} for m in models]
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_status_says_start_ollama_when_it_is_not_running():
    client = OllamaClient(base_url=f"http://127.0.0.1:{_closed_port()}", model="qwen2.5-coder:3b")
    status = client.status()
    assert status["state"] == "offline"
    assert status["message"].startswith("AI offline - start Ollama")


def test_status_names_the_pull_command_when_the_model_is_missing():
    server = _fake_ollama(["qwen2.5-coder:7b"])  # same family, wrong size
    try:
        client = OllamaClient(base_url=f"http://127.0.0.1:{server.server_port}", model="qwen2.5-coder:3b")
        status = client.status()
    finally:
        server.shutdown()
    assert status["state"] == "no_model"
    assert "ollama pull qwen2.5-coder:3b" in status["message"]


def test_status_is_ready_when_the_model_is_pulled():
    server = _fake_ollama(["qwen2.5-coder:3b"])
    try:
        client = OllamaClient(base_url=f"http://127.0.0.1:{server.server_port}", model="qwen2.5-coder:3b")
        status = client.status()
    finally:
        server.shutdown()
    assert status["state"] == "ready" and status["message"] == ""


def test_ai_status_endpoint_never_fails_without_ollama(monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_OLLAMA_URL", f"http://127.0.0.1:{_closed_port()}")
    r = _client().get("/ai/status", headers=H)
    assert r.status_code == 200
    assert r.json()["state"] == "offline"


def test_startup_survives_a_missing_ollama(monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_OLLAMA_URL", f"http://127.0.0.1:{_closed_port()}")
    webapp._log_ai_health()  # must not raise
    with _client() as c:
        assert c.get("/health").status_code == 200


# --------------------------------------------------------------------------- #
#  Model choice
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ram, model", [
    (None, "qwen2.5-coder:3b"), (8, "qwen2.5-coder:3b"), (12, "qwen2.5-coder:3b"),
    (16, "qwen2.5-coder:7b"), (31.9, "qwen2.5-coder:7b"), (32, "qwen2.5-coder:14b"),
])
def test_model_is_picked_by_ram(ram, model):
    assert pick_model_for_ram(ram) == model


def test_env_model_beats_the_ram_pick(monkeypatch):
    monkeypatch.setenv("SETUP_MANAGER_OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setattr(ollama_client, "total_ram_gb", lambda: 64.0)
    assert OllamaClient().model == "llama3.2:3b"


def test_small_models_are_allowed(monkeypatch):
    monkeypatch.delenv("SETUP_MANAGER_OLLAMA_ALLOWED_MODELS", raising=False)
    allowed = OllamaClient().allowed_models
    assert {"qwen2.5:3b", "llama3.2:3b", "qwen2.5-coder:3b"} <= allowed
    assert OllamaClient.DEFAULT_MODEL == "qwen2.5-coder:3b"


def test_ram_reading_does_not_crash():
    value = ollama_client.total_ram_gb()
    assert value is None or value > 0


# --------------------------------------------------------------------------- #
#  The doctor learns verified fixes
# --------------------------------------------------------------------------- #
def test_a_verified_fix_is_recorded_once(tmp_path):
    from doctor.learning import record_learned_fix

    target = tmp_path / "known.json"
    target.write_text(json.dumps({"EADDRINUSE": {"name": "a", "cause": "b", "fix": "c"}}), encoding="utf-8")
    assert record_learned_fix("Broken links", "Broken links", "Dead symlinks", "Remove them", path=target)
    assert not record_learned_fix("broken LINKS", "x", "y", "z", path=target)  # dedupe
    assert not record_learned_fix("EADDRINUSE", "x", "y", "z", path=target)     # never overwrite
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["Broken links"] == {"name": "Broken links", "cause": "Dead symlinks", "fix": "Remove them"}
    assert data["EADDRINUSE"]["name"] == "a"
    assert not list(tmp_path.glob(".known_failures-*"))  # no temp files left behind


def test_an_unreadable_knowledge_file_is_left_alone(tmp_path):
    from doctor.learning import record_learned_fix

    target = tmp_path / "known.json"
    target.write_text("{not json", encoding="utf-8")
    assert not record_learned_fix("Sig", "n", "c", "f", path=target)
    assert target.read_text(encoding="utf-8") == "{not json"


def _fake_doctor(monkeypatch, problems_before, problems_after):
    from doctor import build_doctor
    from doctor.diagnostics import Diagnostic

    calls = iter([problems_before, problems_after])
    recorded = []
    monkeypatch.setattr(build_doctor.BuildDoctor, "_problems", lambda self: {
        i: Diagnostic(id=i, title="Title " + i, status="warn", detail="why", fix="do it")
        for i in next(calls)
    })
    monkeypatch.setattr(build_doctor, "record_learned_fix", lambda *a, **k: recorded.append(a) or True)
    return build_doctor, recorded


def test_doctor_learns_only_when_the_problem_is_gone(monkeypatch, tmp_path):
    from doctor.repairs import Repair

    build_doctor, recorded = _fake_doctor(monkeypatch, ["symlink.broken"], [])
    repair = Repair("r", "Fix", "desc", "symlink.broken", action=lambda: (1, "ok"))
    result = build_doctor.BuildDoctor(tmp_path, log_manager=_NullLog()).apply(repair)
    assert result["verified"] and result["learned"]
    assert recorded[0][0] == "Title symlink.broken"


def test_doctor_does_not_learn_a_fix_that_did_not_work(monkeypatch, tmp_path):
    from doctor.repairs import Repair

    build_doctor, recorded = _fake_doctor(monkeypatch, ["symlink.broken"], ["symlink.broken"])
    repair = Repair("r", "Fix", "desc", "symlink.broken", action=lambda: (1, "ok"))
    result = build_doctor.BuildDoctor(tmp_path, log_manager=_NullLog()).apply(repair)
    assert not result["verified"] and not recorded


class _NullLog:
    def record(self, entry):
        pass


# --------------------------------------------------------------------------- #
#  Project folder and safe root from the dashboard
# --------------------------------------------------------------------------- #
@pytest.fixture
def reset_paths():
    yield
    settings_manager.update_category("paths", {"safe_root": "", "last_project": ""})


def test_safe_root_can_be_changed_and_reset(tmp_path, reset_paths):
    new_root = tmp_path / "projects"
    new_root.mkdir()
    c = _client()
    r = c.post("/settings/safe-root", json={"path": str(new_root)}, headers=H)
    assert r.status_code == 200 and Path(r.json()["safe_root"]) == new_root.resolve()
    # The env root stays allowed beside the dashboard root. A third folder does not.
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    assert c.post("/analyze", json={"path": str(outside)}, headers=H).status_code == 400
    r = c.post("/settings/safe-root", json={"path": ""}, headers=H)
    assert Path(r.json()["safe_root"]) == SAFE_ROOT.resolve()


@pytest.mark.parametrize("bad", ["relative/folder", "/", "/etc", "/does/not/exist"])
def test_unsafe_roots_are_refused(bad, reset_paths):
    r = _client().post("/settings/safe-root", json={"path": bad}, headers=H)
    assert r.status_code == 400
    assert Path(webapp._safe_root()) == SAFE_ROOT.resolve()


def test_home_folder_itself_is_refused(reset_paths):
    r = _client().post("/settings/safe-root", json={"path": str(Path.home())}, headers=H)
    assert r.status_code == 400


def test_safe_root_change_needs_the_token(tmp_path, reset_paths):
    r = _client().post("/settings/safe-root", json={"path": str(tmp_path)})
    assert r.status_code == 401
    assert Path(webapp._safe_root()) == SAFE_ROOT.resolve()


def test_generic_settings_cannot_change_paths(tmp_path, reset_paths):
    r = _client().post("/settings", json={"category": "paths", "settings": {"safe_root": "/"}}, headers=H)
    assert r.status_code == 400


def test_last_project_is_remembered(reset_paths):
    project = SAFE_ROOT / "remember-me"
    project.mkdir(exist_ok=True)
    (project / "requirements.txt").write_text("requests\n", encoding="utf-8")
    c = _client()
    assert c.post("/analyze", json={"path": str(project)}, headers=H).status_code == 200
    assert c.get("/api/config", headers=H).json()["last_project"] == str(project.resolve())


def test_a_last_project_outside_the_root_is_forgotten(tmp_path, reset_paths):
    settings_manager.set("paths", "last_project", str(tmp_path))
    assert _client().get("/api/config", headers=H).json()["last_project"] == ""


def test_open_folder_validates_the_path(tmp_path):
    r = _client().post("/open-folder", json={"path": str(tmp_path)}, headers=H)
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
#  Dry run before installs and repairs
# --------------------------------------------------------------------------- #
def test_plan_lists_the_exact_install_commands():
    project = SAFE_ROOT / "plan-me"
    project.mkdir(exist_ok=True)
    (project / "requirements.txt").write_text("requests\n", encoding="utf-8")
    r = _client().post("/plan", json={"action": "install-dependencies", "path": str(project), "project_type": "python"}, headers=H)
    assert r.status_code == 200
    commands = r.json()["commands"]
    assert any("venv" in c for c in commands)
    assert "pip install --only-binary=:all: -r requirements.txt" in commands[-1]
    assert not (project / "venv").exists()  # the preview ran nothing


def test_plan_refuses_paths_outside_the_root(tmp_path):
    r = _client().post("/plan", json={"action": "install-missing", "path": str(tmp_path)}, headers=H)
    assert r.status_code == 400


def test_plan_and_install_use_the_same_commands():
    project = SAFE_ROOT / "plan-same"
    project.mkdir(exist_ok=True)
    (project / "requirements.txt").write_text("requests\n", encoding="utf-8")
    steps = webapp._dependency_install_commands("python", project)
    assert steps[-1][-2:] == ["-r", "requirements.txt"]


def test_plan_for_a_repair_describes_it():
    project = SAFE_ROOT / "plan-repair"
    project.mkdir(exist_ok=True)
    r = _client().post("/plan", json={"action": "repair", "path": str(project), "repair_id": "clear_pycache"}, headers=H)
    assert r.status_code == 200
    assert "bytecode" in " ".join(r.json()["notes"]).lower()


# --------------------------------------------------------------------------- #
#  Dashboard wiring and the backup terminal menu
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent.parent


def _dashboard_source() -> str:
    """The page markup plus its script, which now lives in static/dashboard.js."""
    static = ROOT / "static"
    return (static / "index.html").read_text(encoding="utf-8") + (static / "dashboard.js").read_text(encoding="utf-8")


def test_dashboard_asks_for_approval_before_installs():
    html = _dashboard_source()
    assert "function planFor(url)" in html and "approve(plan" in html
    for endpoint in ("install-dependencies", "install-missing", "fix-dependencies", "repair"):
        assert endpoint in html


def test_terminal_menu_points_to_the_dashboard():
    source = (ROOT / "setup_manager.py").read_text(encoding="utf-8")
    assert "dashboard (run.bat) is the main way" in source


# --------------------------------------------------------------------------- #
#  Simpler setup view (a Go + Make + Docker project like PhoneInfoga)
# --------------------------------------------------------------------------- #
def _phoneinfoga_like():
    project = SAFE_ROOT / "PhoneInfoga-master"
    (project / "web" / "client").mkdir(parents=True, exist_ok=True)
    (project / "go.mod").write_text("module example.com/phoneinfoga\n\ngo 1.20\n", encoding="utf-8")
    (project / "main.go").write_text("package main\nfunc main(){}\n", encoding="utf-8")
    (project / "Makefile").write_text("build:\n\tgo build .\n", encoding="utf-8")
    (project / "Dockerfile").write_text("FROM golang:1.20\n", encoding="utf-8")
    (project / "web" / "client" / "package.json").write_text('{"name":"client","scripts":{"build":"x"}}', encoding="utf-8")
    return project


def test_guide_marks_a_short_recommended_set():
    project = _phoneinfoga_like()
    g = _client().post("/setup-guide", json={"path": str(project)}, headers=H).json()
    assert all(s["recommended"] for s in g["essential_steps"])
    assert not any(s["recommended"] for s in g["optional_steps"])
    assert len(g["essential_steps"]) <= 5
    assert {s.get("project_type") for s in g["essential_steps"]} <= {None, "go"}
    assert all(s.get("desc") for s in g["essential_steps"] + g["optional_steps"])


def test_guide_mentions_the_web_client_instead_of_adding_buttons():
    project = _phoneinfoga_like()
    g = _client().post("/setup-guide", json={"path": str(project)}, headers=H).json()
    subs = [Path(s["path"]).name for s in g["sub_projects"]]
    assert "client" in subs


def test_go_guide_steps_use_targets_the_runner_understands():
    project = _phoneinfoga_like()
    g = _client().post("/setup-guide", json={"path": str(project)}, headers=H).json()
    params = [s["param"] for s in g["essential_steps"] + g["optional_steps"] if s.get("param")]
    assert "go mod tidy" not in params and "go build ./..." not in params
    assert {"go.mod", "build", "test"} <= set(params)
    assert "make:all" in params


@pytest.mark.parametrize("target, detected, expected", [
    ("make:all", ["go", "make"], ["make", "all"]),
    ("make:clean", ["go", "make", "docker"], ["make", "clean"]),
    ("docker", ["go", "make", "docker"], ["docker", "build", "-t", "phoneinfoga-master", "."]),
    ("make:all; rm -rf /", ["make"], None),
    ("make:all", ["go"], None),
    ("build", ["go", "docker"], None),
])
def test_explicit_targets(target, detected, expected):
    if expected and expected[0] == "make":
        expected = [make_command() or "make", *expected[1:]]
    assert webapp._explicit_target_command(target, detected, Path("PhoneInfoga-master")) == expected


def test_go_dependencies_can_be_installed(monkeypatch):
    project = _phoneinfoga_like()
    monkeypatch.setattr(webapp.shutil, "which", lambda name: "/usr/bin/" + name)
    assert webapp._dependency_install_commands("go", project) == [["go", "mod", "download"]]


def test_go_build_writes_an_exe_when_the_folder_is_a_program():
    project = _phoneinfoga_like()
    command = webapp._go_build_command(project)
    assert command[:3] == ["go", "build", "-o"]
    assert command[-1] == "."
    if webapp.os.name == "nt":
        assert command[3] == "phoneinfoga-master.exe"
    else:
        assert command[3] == "phoneinfoga-master"

    library = SAFE_ROOT / "go-lib"
    library.mkdir(exist_ok=True)
    (library / "go.mod").write_text("module example.com/lib\n\ngo 1.20\n", encoding="utf-8")
    (library / "lib.go").write_text("package lib\n", encoding="utf-8")
    assert webapp._go_build_command(library) == ["go", "build", "./..."]

    guide = _client().post("/setup-guide", json={"path": str(project)}, headers=H).json()
    build = next(step for step in guide["essential_steps"] if step["id"] == "go-build")
    assert build["cmd"] == " ".join(command)


def test_a_second_projects_folder_stays_allowed(tmp_path):
    """The dashboard root can be E:\\Programs while Sherlock still lives on the old drive."""
    chosen = tmp_path / "Programs"
    chosen.mkdir()
    program = chosen / "PhoneInfoga"
    program.mkdir()
    (program / "go.mod").write_text("module example.com/phone\n\ngo 1.20\n", encoding="utf-8")
    settings_manager.set("paths", "safe_root", str(chosen))
    try:
        assert Path(webapp._validate_path(str(program))) == program.resolve()
        older = SAFE_ROOT / "sherlock-source"
        older.mkdir()
        assert Path(webapp._validate_path(str(older))) == older.resolve()
        refused = _client().post("/setup-guide", json={"path": str(tmp_path)}, headers=H)
        assert refused.status_code == 400
    finally:
        settings_manager.set("paths", "safe_root", "")


def test_a_go_module_line_that_disagrees_with_imports_is_explained(tmp_path):
    project = tmp_path / "phone"
    project.mkdir()
    (project / "go.mod").write_text(
        "module github.com/sundowndev/phoneinfoga/v3\n\ngo 1.20\n",
        encoding="utf-8",
    )
    (project / "main.go").write_text(
        'package main\n\nimport "github.com/sundowndev/phoneinfoga/v2/cmd"\n\nfunc main() {}\n',
        encoding="utf-8",
    )
    message = webapp._go_module_mismatch(project)
    assert "phoneinfoga/v3" in message
    assert "phoneinfoga/v2" in message
    assert webapp._go_module_mismatch(_phoneinfoga_like()) is None


def test_dashboard_has_recommended_and_advanced_sections():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for marker in ('id="runRecommended"', 'id="advanced"', 'id="moreCard"', 'id="approve"', 'id="statusbar"', 'id="explorer"', 'id="browse"', 'id="guideCard"', 'id="guideDo"'):
        assert marker in html
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")  # no CDN, works offline


def test_csp_lets_the_dashboard_script_run():
    """The page loads its script from /static, so no inline script is allowed or needed."""
    import re

    c = _client()
    csp = c.get("/").headers["content-security-policy"]
    script_src = next(part for part in csp.split(";") if part.strip().startswith("script-src "))
    assert script_src.split() == ["script-src", "'self'"]
    assert "script-src-attr 'none'" in csp
    page = c.get("/").text
    assert re.search(r'<script src="/static/dashboard\.js(\?v=[\w.-]+)?"></script>', page)
    assert c.get("/static/dashboard.js").status_code == 200


def test_the_dashboard_has_no_inline_script_or_handlers():
    import re

    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert 'src="/static/' in tag, tag
    assert not re.search(r"<script\b[^>]*>\s*[^<\s]", html), "inline script"
    assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", html, re.I), "inline handler"
    assert "javascript:" not in html
