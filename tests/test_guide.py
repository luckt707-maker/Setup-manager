"""The guide recommends a real setup step. Ollama may pick one; it cannot add one."""
import uuid

from fastapi.testclient import TestClient

import app as webapp
from conftest import ROOT, SAFE_ROOT
from core.guide import advise, apply_model_reply, clean_why, present
from ollama_client import OllamaError

H = {"X-Setup-Token": webapp.API_TOKEN}

STEPS = [
    {
        "id": "security-scan",
        "label": "🛡️ Security scan",
        "desc": "Check the project before running anything",
        "action": "security-scan",
    },
    {
        "id": "python-venv",
        "label": "Create virtual environment",
        "desc": "Set up an isolated Python environment",
        "cmd": "python -m venv venv",
        "endpoint": "/task/setup/python",
    },
    {
        "id": "python-install",
        "label": "Install dependencies",
        "desc": "Install packages from requirements.txt",
        "cmd": "pip install -r requirements.txt",
        "endpoint": "/task/install-dependencies/python",
    },
]


def _client():
    return TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def _python_project():
    project = SAFE_ROOT / f"guide-py-{uuid.uuid4().hex[:8]}"
    project.mkdir()
    (project / "requirements.txt").write_text("requests\n", encoding="utf-8")
    return project


def _post(payload):
    response = _client().post("/guide/advice", json=payload, headers=H)
    return response


def _advice(**kwargs):
    return advise(steps=STEPS, detected=["python"], **kwargs)


# --------------------------------------------------------------------------- #
#  Built-in recommendations
# --------------------------------------------------------------------------- #
def test_browsing_has_nothing_to_run():
    advice = advise(activity="browse", steps=STEPS)
    assert advice["action"] is None
    assert advice["source"] == "rules"
    assert advice["headline"] == "Pick a project"


def test_a_running_task_names_the_step_and_hides_the_button():
    advice = advise(activity="running", task_label="Security scan", steps=STEPS)
    assert advice["action"] is None
    assert "Security scan" in advice["headline"]


def test_a_failed_step_is_offered_again():
    advice = _advice(activity="failed", task_label="Create virtual environment")
    assert advice["action"]["id"] == "python-venv"
    assert "did not finish" in advice["headline"]


def test_a_finished_step_advances_to_the_next_one():
    advice = _advice(activity="finished", task_label="Security scan")
    assert advice["action"]["id"] == "python-venv"
    assert advice["after"][0] == "Install dependencies"


def test_the_last_finished_step_has_no_further_action():
    advice = _advice(activity="finished", task_label="Install dependencies")
    assert advice["action"] is None
    assert advice["headline"] == "The recommended steps are done"


def test_a_fresh_project_starts_at_the_first_step():
    advice = _advice(activity="project")
    assert advice["action"]["id"] == "security-scan"
    assert "python" in advice["detail"]


def test_an_unknown_activity_falls_back_to_browsing():
    assert advise(activity="somewhere-else")["activity"] == "browse"


# --------------------------------------------------------------------------- #
#  What the model is allowed to change
# --------------------------------------------------------------------------- #
def _project_advice():
    return _advice(activity="project")


def test_an_unknown_action_with_no_reason_is_ignored():
    advice = _project_advice()
    assert apply_model_reply(advice, '{"action_id":"format-disk"}') is advice


def test_a_real_action_and_its_reason_are_kept():
    updated = apply_model_reply(
        _project_advice(),
        '```json\n{"action_id":"python-venv","why":"The venv has to exist first."}\n```',
    )
    assert updated["source"] == "ollama"
    assert updated["action"]["id"] == "python-venv"
    assert updated["action"]["cmd"] == "python -m venv venv"
    assert updated["headline"] == "Suggested next step"
    assert updated["detail"] == "The venv has to exist first."


def test_an_invented_action_cannot_replace_the_button():
    updated = apply_model_reply(
        _project_advice(),
        '{"action_id":"format-disk","why":"Run `format C:` now","cmd":"format C:"}',
    )
    assert updated["source"] == "ollama"
    assert updated["action"]["id"] == "security-scan"
    assert "`" not in updated["detail"]
    assert "format C:" not in updated["action"].get("cmd", "")


def test_none_keeps_the_button_on_a_fresh_project():
    updated = apply_model_reply(_project_advice(), '{"action_id":"none","why":"Wait."}')
    assert updated["action"]["id"] == "security-scan"


def test_none_hides_the_button_after_a_failure():
    failed = _advice(activity="failed", task_label="Security scan")
    updated = apply_model_reply(failed, '{"action_id":"none","why":"Read the output first."}')
    assert updated["action"] is None
    assert updated["detail"] == "Read the output first."


def test_bad_json_keeps_the_built_in_advice():
    advice = _project_advice()
    assert apply_model_reply(advice, "just do whatever you like") is advice


def test_a_reason_cannot_carry_a_command_or_run_long():
    cleaned = clean_why("  please  `rm -rf /`  " + ("x" * 400))
    assert "`" not in cleaned
    assert "  " not in cleaned
    assert len(cleaned) == 280


def test_the_choice_list_stays_on_the_server():
    shown = present(_project_advice())
    assert "choices" not in shown
    assert shown["action"]["id"] == "security-scan"


# --------------------------------------------------------------------------- #
#  HTTP
# --------------------------------------------------------------------------- #
def test_browsing_needs_no_project():
    response = _post({"activity": "browse", "path": "C:\\does\\not\\exist"})
    assert response.status_code == 200
    body = response.json()
    assert body["action"] is None
    assert body["source"] == "rules"
    assert "choices" not in body


def test_a_project_without_a_path_asks_the_user_to_pick_one():
    body = _post({"activity": "finished"}).json()
    assert body["activity"] == "browse"
    assert body["action"] is None


def test_a_python_project_starts_with_the_security_scan():
    project = _python_project()
    body = _post({"path": str(project), "activity": "project"}).json()
    assert body["source"] == "rules"
    assert body["action"]["id"] == "security-scan"
    assert "Create virtual environment" in body["after"]
    assert "python" in body["detail"]


def test_finishing_the_scan_recommends_the_virtual_environment():
    project = _python_project()
    body = _post({
        "path": str(project),
        "activity": "finished",
        "task_label": "Security scan",
        "task_status": "completed",
    }).json()
    assert body["action"]["id"] == "python-venv"
    assert body["after"][0] == "Install dependencies"


def test_a_path_outside_the_projects_root_is_refused(tmp_path):
    response = _post({"path": str(tmp_path), "activity": "project"})
    assert response.status_code == 400


def _install_model(monkeypatch, reply, state="ready"):
    """Stand in for Ollama. Startup may ask for status; chat is the guide call."""
    calls = {"chat": 0, "timeouts": []}

    class Fake:
        def __init__(self, timeout=None):
            calls["timeouts"].append(timeout)

        def status(self):
            return {"state": state, "model": "qwen2.5-coder:3b"}

        def chat(self, messages, temperature=None, max_tokens=None):
            calls["chat"] += 1
            calls["messages"] = messages
            calls["temperature"] = temperature
            calls["max_tokens"] = max_tokens
            if isinstance(reply, Exception):
                raise reply
            return {"reply": reply}

    monkeypatch.setattr(webapp, "OllamaClient", Fake)
    return calls


def test_a_running_task_does_not_analyze_or_call_the_model(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("this should not run while a task is in progress")

    monkeypatch.setattr(webapp.ProjectOrchestrator, "analyze", explode)
    calls = _install_model(monkeypatch, "should not be asked")
    project = _python_project()
    body = _post({
        "path": str(project),
        "activity": "running",
        "task_label": "Security scan",
        "use_ai": True,
    }).json()
    assert calls["chat"] == 0
    assert body["action"] is None
    assert "Security scan" in body["headline"]


def test_use_ai_false_never_calls_ollama(monkeypatch):
    calls = _install_model(monkeypatch, "should not be asked")
    project = _python_project()
    body = _post({"path": str(project), "activity": "project", "use_ai": False}).json()
    assert calls["chat"] == 0
    assert body["source"] == "rules"
    assert body["action"]["id"] == "security-scan"


def test_a_ready_model_can_choose_one_of_the_real_steps(monkeypatch):
    calls = _install_model(
        monkeypatch,
        '{"action_id":"python-venv","why":"Create the environment before installing packages."}',
    )
    project = _python_project()
    body = _post({"path": str(project), "activity": "project", "use_ai": True}).json()
    assert calls["chat"] == 1
    assert 20 in calls["timeouts"]
    assert calls["temperature"] == 0.2
    prompt = calls["messages"][-1]["content"]
    assert "python-venv" in prompt
    assert "Do not invent actions or commands." in prompt
    assert body["source"] == "ollama"
    assert body["action"]["id"] == "python-venv"
    assert body["action"]["cmd"] == "python -m venv venv"
    assert body["headline"] == "Suggested next step"


def test_a_model_command_is_not_turned_into_an_action(monkeypatch):
    _install_model(
        monkeypatch,
        '{"action_id":"format-disk","why":"Run `format C:`","cmd":"format C:"}',
    )
    project = _python_project()
    body = _post({"path": str(project), "activity": "project", "use_ai": True}).json()
    assert body["source"] == "ollama"
    assert body["action"]["id"] == "security-scan"
    assert "`" not in body["detail"]


def test_ollama_offline_keeps_the_built_in_step(monkeypatch):
    calls = _install_model(monkeypatch, "should not be asked", state="offline")
    project = _python_project()
    body = _post({"path": str(project), "activity": "project", "use_ai": True}).json()
    assert calls["chat"] == 0
    assert body["source"] == "rules"
    assert body["action"]["id"] == "security-scan"


def test_a_broken_model_reply_keeps_the_built_in_step(monkeypatch):
    _install_model(monkeypatch, OllamaError("connection reset"))
    project = _python_project()
    body = _post({"path": str(project), "activity": "project", "use_ai": True}).json()
    assert body["source"] == "rules"
    assert body["action"]["id"] == "security-scan"


def test_the_dashboard_asks_the_guide_and_explains_with_ollama():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8") + (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
    c = _client()
    served = c.get("/").text + c.get("/static/dashboard.js").text
    for marker in ('id="guideCard"', 'id="guideDo"', 'id="guideAsk"', "/guide/advice", "Explain with Ollama", "noteTask"):
        assert marker in html
        assert marker in served
