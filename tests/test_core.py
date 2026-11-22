"""Core engine tests: detection, execution, setup handlers and the API."""
import sys
import tempfile
from pathlib import Path

from core import (
    CommandExecutor,
    ProjectDetector,
    QoLExtensionsRegistry,
    SecurityScanner,
    Setup,
    WorkspaceDetector,
)


def _proj(name, files):
    d = Path(tempfile.mkdtemp()) / name
    d.mkdir(parents=True, exist_ok=True)
    for fname, text in files.items():
        path = d / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return d


def test_detects_types():
    d = _proj("multi", {"requirements.txt": "requests\n", "package.json": '{"name":"x"}'})
    assert set(ProjectDetector(str(d)).detect()) >= {"python", "nodejs"}


def test_a_python_child_can_print_an_emoji():
    """Sherlock's welcome banner contains an emoji. cp1252 must not kill it."""
    lines = []
    ok = CommandExecutor.run(
        [sys.executable, "-c", "print('\\U0001f40d')"],
        callback=lines.append,
        timeout=30,
    )
    assert ok, lines
    assert any("\U0001f40d" in line for line in lines), lines


def test_executor_streams_and_reports_failure():
    lines = []
    assert CommandExecutor.run([sys.executable, "-c", "print('hi')"], callback=lines.append)
    assert any("hi" in line for line in lines)
    assert not CommandExecutor.run([sys.executable, "-c", "import sys; sys.exit(3)"], callback=lines.append)


def test_executor_timeout_kills():
    assert not CommandExecutor.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)


def test_setup_methods_accept_callback_and_never_block_on_input(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("input() called")))
    s = Setup(".")
    for name in ("setup_python", "setup_nodejs", "setup_dotnet", "setup_git", "setup_make", "setup_ruby"):
        assert "callback" in getattr(s, name).__code__.co_varnames


def test_setup_defaults_to_non_interactive():
    """The web API must never be able to make Setup block on input()."""
    assert Setup(".").interactive is False
    assert Setup(".", interactive=True).interactive is True


def test_registry_returns_extensions_for_a_type():
    assert QoLExtensionsRegistry.get_extensions("python", "linters")
    assert QoLExtensionsRegistry.get_extensions("python", "no-such-category") == []


def test_security_scan_reports_findings():
    d = _proj("scan", {"risky.py": "import os\nos.system('rm -rf /')\n"})
    result = SecurityScanner.scan(d)
    assert result["files_scanned"] >= 1
    assert isinstance(result["findings"], list)


def test_security_scan_flags_committed_private_keys():
    d = _proj("keys", {
        "id_rsa.py": "KEY = \"-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----\"\n",
    })
    result = SecurityScanner.scan(d)
    rules = {f["rule"] for f in result["findings"]}
    assert "SEC009" in rules
    finding = next(f for f in result["findings"] if f["rule"] == "SEC009")
    assert finding["severity"] == "critical"


def test_security_scan_flags_hardcoded_credentials():
    d = _proj("creds", {"conf.py": 'password = "hunter2hunter2"\n'})
    result = SecurityScanner.scan(d)
    rules = {f["rule"] for f in result["findings"]}
    assert "SEC008" in rules


def test_security_scan_ignores_ordinary_words():
    """The words that flooded the Sherlock scan are not persistence or ciphers."""
    d = _proj("words", {"notes.py": (
        '"""search for usernames at social networks"""\n'
        'print("that task is not real")\n'
        'path = os.path.join(here, "../sherlock/resources/data.json")\n'
        'raise ValueError("impossible...")\n'
        'text = proc_out.decode()\n'
    )})
    rules = {f["rule"] for f in SecurityScanner.scan(d)["findings"]}
    assert "BD002" not in rules
    assert "FR004" not in rules
    assert "OBF006" not in rules


def test_security_scan_still_flags_real_persistence_and_ciphers():
    d = _proj("real", {"bad.py": (
        'import os\n'
        'os.system("schtasks /create /tn x /tr calc.exe")\n'
        'os.system("crontab -l")\n'
        'os.system("at 14:00 malware.exe")\n'
        'os.system("attrib +h secret.txt")\n'
        'rot13("payload")\n'
    )})
    rules = {f["rule"] for f in SecurityScanner.scan(d)["findings"]}
    assert {"BD002", "FR004", "OBF006"} <= rules


def test_security_fix_switches_shell_true_on_lists_only():
    d = _proj("shells", {"run.py": (
        "import subprocess\n"
        'subprocess.run(["py", "-m", "tool"], shell=True)\n'
        'subprocess.check_output(f"py -m tool {args}", shell=True)\n'
    )})
    before = [f for f in SecurityScanner.scan(d)["findings"] if f["rule"] == "SEC007"]
    assert len(before) == 2
    assert sum(1 for f in before if f["fixable"]) == 1

    result = SecurityScanner.apply_fixes(d)
    text = (d / "run.py").read_text(encoding="utf-8")
    assert 'subprocess.run(["py", "-m", "tool"], shell=False)' in text
    assert 'shell=True' in text
    remaining = [f for f in result["findings"] if f["rule"] == "SEC007"]
    assert len(remaining) == 1
    assert remaining[0]["fixable"] is False
    assert result["fix_result"]["files_changed"] == 1


def test_security_fix_endpoint_applies_safe_edits(monkeypatch):
    d = _proj("api-fix", {"run.py": 'import subprocess\nsubprocess.run(["echo", "hi"], shell=True)\n'})
    c, w = _client()
    monkeypatch.setattr(w, "_validate_path", lambda path: str(d))
    response = c.post(
        "/security-scan/fix",
        json={"path": str(d)},
        headers={"X-Setup-Token": w.API_TOKEN},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["fix_result"]["files_changed"] == 1
    assert body["fixable_count"] == 0
    assert "shell=False" in (d / "run.py").read_text(encoding="utf-8")


def test_security_page_offers_fix():
    static = Path(__file__).resolve().parent.parent / "static"
    html = (static / "index.html").read_text(encoding="utf-8") + (static / "dashboard.js").read_text(encoding="utf-8")
    assert "Fix these" in html
    assert "/security-scan/fix" in html


def test_workspace_detector_finds_frontend_and_backend():
    root = _proj("ws", {})
    (root / "api").mkdir()
    (root / "api" / "requirements.txt").write_text("fastapi\n")
    (root / "web").mkdir()
    (root / "web" / "package.json").write_text('{"name":"web"}')

    found = WorkspaceDetector(root).detect()
    assert found["backend"] is not None
    assert found["backend_type"] == "python"
    assert found["frontend"] is not None


def test_workspace_detector_on_missing_folder_is_empty():
    found = WorkspaceDetector(Path("no-such-folder-anywhere")).detect()
    assert found["backend"] is None and found["frontend"] is None


# ---- API-level tests (need fastapi + httpx installed) ----
def _client():
    from fastapi.testclient import TestClient
    import app as webapp
    return TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)), webapp


def test_api_requires_token():
    c, _ = _client()
    assert c.get("/projects").status_code == 401
    assert c.get("/health").status_code == 200


def test_api_blocks_foreign_host_and_origin():
    c, w = _client()
    h = {"X-Setup-Token": w.API_TOKEN}
    assert c.get("/projects", headers={**h, "Host": "evil.example"}).status_code == 400
    assert c.get("/projects", headers={**h, "Origin": "http://evil.example"}).status_code == 403


def test_api_rejects_paths_outside_root_and_empty_path():
    c, w = _client()
    h = {"X-Setup-Token": w.API_TOKEN}
    outside = tempfile.mkdtemp()
    assert c.post("/analyze", json={"path": outside}, headers=h).status_code == 400
    assert c.post("/task/outdated", json={"path": ""}, headers=h).status_code == 422


def test_custom_command_needs_confirm():
    c, w = _client()
    r = c.post("/task/custom", json={"command": "echo hi"}, headers={"X-Setup-Token": w.API_TOKEN})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
#  Quality-check result classification
# --------------------------------------------------------------------------- #

class _FakeExecutor:
    """Stands in for CommandExecutor so the checks can be driven precisely.

    ``results`` maps a check name to the exit code that check should report.
    """

    def __init__(self, results):
        self.results = results
        self.last_return_code = None
        self.calls = []

    def check_installed(self, command):
        return True

    def run_with_code(self, command, **kwargs):
        name = command[2] if "-m" in command else command[0]
        self.calls.append((name, kwargs.get("skip_exit_codes")))
        code = self.results.get(name, 0)
        self.last_return_code = code
        return code == 0, code


def _plain_project():
    """A project that does not use pytest.

    The repository itself uses pytest, and a pytest project no longer runs
    through unittest as well - so the tests about the two runners need a
    project where both of them apply.
    """
    return _proj("plain", {"tests/test_demo.py": "def test_ok():\n    assert True\n"})


def _run_checks(results, project_type="python"):
    setup = Setup(str(_plain_project()))
    setup.executor = _FakeExecutor(results)
    # Availability is probed for real (it asks the venv's interpreter), and
    # tools that are not installed there are skipped before they ever run.
    setup._check_available = lambda command: True
    return setup.run_quality_checks(project_type)


def test_test_runner_with_no_tests_is_a_skip_not_a_failure():
    """pytest and unittest exit 5 when they collected nothing to run."""
    results = _run_checks({"pytest": 5, "unittest": 5})
    assert results["pytest"] == "skip"
    assert results["unittest"] == "skip"


def test_failing_test_runner_is_still_a_failure():
    """Exit code 1 means the tests ran and failed - that must stay a failure."""
    results = _run_checks({"pytest": 1, "unittest": 1})
    assert results["pytest"] == "fail"
    assert results["unittest"] == "fail"


def test_passing_test_runner_is_a_pass():
    results = _run_checks({"pytest": 0, "unittest": 0})
    assert results["pytest"] == "pass"
    assert results["unittest"] == "pass"


def test_non_tester_exit_five_is_still_a_failure():
    """Exit code 5 only means 'nothing to run' for a test runner."""
    results = _run_checks({"pylint": 5})
    assert results["pylint"] == "fail"


def test_skip_exit_codes_are_offered_only_to_test_runners():
    setup = Setup(str(_plain_project()))
    setup.executor = _FakeExecutor({})
    setup._check_available = lambda command: True
    setup.run_quality_checks("python")
    offered = {name: codes for name, codes in setup.executor.calls if codes}
    assert offered.get("pytest") == {5}
    assert offered.get("unittest") == {5}
    assert "pylint" not in offered


def test_missing_tool_is_reported_as_a_skip():
    """A tool that is not installed is skipped, not failed."""
    setup = Setup(str(_plain_project()))
    setup.executor = _FakeExecutor({})
    setup._check_available = lambda command: False
    results = setup.run_quality_checks("python")
    assert set(results.values()) == {"skip"}


def test_availability_probe_asks_the_interpreter_not_the_path():
    """`<python> -m tool` is available only if the module is importable.

    The interpreter is always present, so a PATH check would wrongly report
    every Python tool as installed. A project with no virtual environment
    has no interpreter of its own; the probe still has to be given a real one.
    """
    setup = Setup(".")
    assert setup._venv_python() is None
    assert setup._check_available([sys.executable, "-m", "pytest"]) is True
    assert setup._check_available([sys.executable, "-m", "a_module_that_is_not_installed"]) is False


def test_executor_reports_the_exit_code():
    success, code = CommandExecutor.run_with_code(
        [sys.executable, "-c", "import sys; sys.exit(7)"]
    )
    assert success is False
    assert code == 7
    success, code = CommandExecutor.run_with_code([sys.executable, "-c", "pass"])
    assert success is True
    assert code == 0
