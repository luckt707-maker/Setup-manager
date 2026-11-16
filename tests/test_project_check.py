"""Missing files and folders, and updates that are downloaded only when safe."""
import base64
import hashlib
import importlib.util
import json
import urllib.error
import urllib.request
import uuid

import pytest

from fastapi.testclient import TestClient

import app as webapp
from conftest import ROOT, SAFE_ROOT
from core import project_check

H = {"X-Setup-Token": webapp.API_TOKEN}
WHEEL = b"wheel-bytes-not-installed"
WHEEL_SHA = hashlib.sha256(WHEEL).hexdigest()
WHEEL_URL = "https://files.pythonhosted.org/packages/ab/cd/requests-2.32.3-py3-none-any.whl"
TARBALL = b"tarball-bytes-not-installed"
TARBALL_URL = "https://registry.npmjs.org/left-pad/-/left-pad-1.2.0.tgz"
TARBALL_SRI = "sha512-" + base64.b64encode(hashlib.sha512(TARBALL).digest()).decode()


def _project(tmp_path, files):
    project = tmp_path / "proj"
    project.mkdir()
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


def _wheel_release(url=WHEEL_URL, digest=WHEEL_SHA, yanked=False, packagetype="bdist_wheel"):
    return {
        "packagetype": packagetype,
        "filename": "requests-2.32.3-py3-none-any.whl",
        "url": url,
        "yanked": yanked,
        "digests": {"sha256": digest},
    }


def _pypi(files=None, name="requests", version="2.32.3"):
    return {
        "info": {"name": name, "version": version},
        "releases": {version: files if files is not None else [_wheel_release()]},
    }


def _fetch(payloads):
    calls = []

    def fetch(url, hosts, limit=project_check.MAX_BYTES, timeout=20):
        calls.append(url)
        if url not in payloads:
            raise ValueError("unexpected " + url)
        data = payloads[url]
        if isinstance(data, bytes):
            return data
        return json.dumps(data).encode()

    return calls, fetch


def _script():
    spec = importlib.util.spec_from_file_location(
        "check_project_script", ROOT / "scripts" / "check_project.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
#  Missing files and folders
# --------------------------------------------------------------------------- #
def test_included_files_copy_sources_and_folders_are_reported(tmp_path):
    project = _project(tmp_path, {
        "requirements.txt": "-r requirements-dev.txt\n",
        "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\nreadme = "README.md"\n',
        "Dockerfile": "FROM python:3.11\nCOPY app.py /app/app.py\nADD https://example.com/x.tar /tmp/x\n",
        "Makefile": "include config.mk\n",
        "package.json": '{"main": "missing.js", "dependencies": {}}\n',
        "pkg/__init__.py": "",
        "pkg/app.py": "from . import gone\n",
    })

    missing = {(item["kind"], item["path"]) for item in project_check.missing_paths(project)}

    assert ("file", "requirements-dev.txt") in missing
    assert ("file", "README.md") in missing
    assert ("file", "app.py") in missing
    assert ("file", "config.mk") in missing
    assert ("file", "missing.js") in missing
    assert ("folder", "node_modules") in missing
    assert ("folder", "venv") in missing
    assert any(item[0] == "file" and "gone" in item[1] for item in missing)
    assert not any("example.com" in item[1] for item in missing)


def test_a_byte_order_mark_does_not_hide_an_included_file(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "requirements.txt").write_bytes(b"\xef\xbb\xbf-r missing.txt\n")

    missing = project_check.missing_paths(project)

    assert any(item["path"] == "missing.txt" for item in missing)


def test_an_include_outside_the_project_is_not_opened(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    project = _project(tmp_path, {"requirements.txt": "-r ../secret.txt\n"})

    missing = project_check.missing_paths(project)

    assert any("outside" in item["reason"] for item in missing)
    assert all("secret" not in item["reason"] for item in missing)


def test_an_import_that_is_not_declared_is_not_downloaded(tmp_path):
    project = _project(tmp_path, {"app.py": "import requests\n"})

    def explode(*_args, **_kwargs):
        raise AssertionError("the registry should not be asked")

    report = project_check.check_project(project, explode)
    assert report["updates"] == []
    assert report["downloads"] == []


# --------------------------------------------------------------------------- #
#  What may be downloaded
# --------------------------------------------------------------------------- #
def test_a_matching_wheel_is_safe_and_the_file_is_not_fetched_during_the_check(tmp_path):
    project = _project(tmp_path, {"requirements.txt": "requests>=2\n"})
    calls, fetch = _fetch({"https://pypi.org/pypi/requests/json": _pypi(), WHEEL_URL: WHEEL})

    report = project_check.check_project(project, fetch)

    assert calls == ["https://pypi.org/pypi/requests/json"]
    update = report["updates"][0]
    assert update["safe"] is True
    assert update["version"] == "2.32.3"
    assert update["url"] == WHEEL_URL
    assert not (project / ".setup-manager").exists()


def test_a_pinned_or_source_or_yanked_or_foreign_release_is_not_downloaded(tmp_path):
    cases = {
        "pin": ("requests==2.0.0\n", _pypi()),
        "sdist": ("requests\n", _pypi(files=[_wheel_release(
            url="https://files.pythonhosted.org/packages/ab/requests-2.32.3.tar.gz",
            packagetype="sdist",
        )])),
        "yanked": ("requests\n", _pypi(files=[_wheel_release(yanked=True)])),
        "foreign": ("requests\n", _pypi(files=[_wheel_release(url="https://evil.example/requests.whl")])),
        "imposter": ("requests\n", _pypi(name="not-requests")),
    }
    for name, (requirements, payload) in cases.items():
        project = tmp_path / name
        project.mkdir()
        (project / "requirements.txt").write_text(requirements, encoding="utf-8")
        calls, fetch = _fetch({"https://pypi.org/pypi/requests/json": payload, WHEEL_URL: WHEEL})
        report = project_check.check_project(project, fetch)
        assert report["updates"][0]["safe"] is False, name
        assert all("evil.example" not in url for url in calls), name
        assert WHEEL_URL not in calls, name


def test_a_safe_wheel_is_saved_only_when_the_checksum_matches(tmp_path):
    project = _project(tmp_path, {"requirements.txt": "requests>=2\n"})
    payloads = {"https://pypi.org/pypi/requests/json": _pypi(), WHEEL_URL: WHEEL}
    _calls, fetch = _fetch(payloads)

    saved = project_check.download_safe_updates(project, fetch)
    target = project / ".setup-manager" / "updates" / "python" / "requests-2.32.3-py3-none-any.whl"
    assert target.read_bytes() == WHEEL
    assert saved["downloads"][0]["downloaded"] is True
    assert "not installed" in saved["downloads"][0]["reason"]

    payloads[WHEEL_URL] = b"tampered"
    _calls, fetch = _fetch(payloads)
    refused = project_check.download_safe_updates(project, fetch)
    assert refused["downloads"][0]["downloaded"] is False
    assert refused["downloads"][0]["reason"] == "the checksum did not match the registry"


def test_an_npm_tarball_from_the_official_registry_can_be_saved(tmp_path):
    project = _project(tmp_path, {"package.json": '{"dependencies": {"left-pad": "^1.0.0"}}\n'})
    meta = {
        "name": "left-pad",
        "versions": {"1.2.0": {"dist": {"tarball": TARBALL_URL, "integrity": TARBALL_SRI}}},
    }
    calls, fetch = _fetch({"https://registry.npmjs.org/left-pad": meta, TARBALL_URL: TARBALL})

    report = project_check.download_safe_updates(project, fetch)

    assert "https://registry.npmjs.org/left-pad" in calls
    assert TARBALL_URL in calls
    saved = project / ".setup-manager" / "updates" / "node" / "left-pad-1.2.0.tgz"
    assert saved.read_bytes() == TARBALL
    assert report["downloads"][0]["downloaded"] is True


def test_a_git_dependency_and_a_foreign_tarball_are_not_fetched(tmp_path):
    project = _project(tmp_path, {
        "package.json": '{"dependencies": {"evil": "git+https://github.com/evil/evil.git", "left-pad": "^1.0.0"}}\n',
    })
    meta = {
        "name": "left-pad",
        "versions": {
            "1.2.0": {
                "dist": {
                    "tarball": "https://evil.example/left-pad-1.2.0.tgz",
                    "integrity": TARBALL_SRI,
                }
            }
        },
    }
    calls, fetch = _fetch({"https://registry.npmjs.org/left-pad": meta})

    report = project_check.check_project(project, fetch)

    held = {item["name"]: item for item in report["updates"]}
    assert held["evil"]["safe"] is False
    assert held["left-pad"]["safe"] is False
    assert all("evil.example" not in url and "github.com" not in url for url in calls)


def test_plain_http_and_a_redirect_off_the_registry_are_refused():
    with pytest.raises(ValueError):
        project_check.fetch_url("http://pypi.org/pypi/requests/json", project_check.PYPI_METADATA_HOSTS)
    handler = project_check._RefuseRedirect({"pypi.org"})
    request = urllib.request.Request("https://pypi.org/pypi/requests/json")
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/steal")


def test_the_script_does_not_download_unless_asked(tmp_path, monkeypatch):
    project = _project(tmp_path, {})
    module = _script()
    monkeypatch.setattr(module, "check_project", lambda path: {
        "path": str(path), "missing": [], "updates": [], "downloads": [],
    })

    def explode(path):
        raise AssertionError("download")

    monkeypatch.setattr(module, "download_safe_updates", explode)
    assert module.main([str(project)]) == 0

    monkeypatch.setattr(module, "check_project", explode)
    monkeypatch.setattr(module, "download_safe_updates", lambda path: {
        "path": str(path),
        "missing": [],
        "updates": [{"name": "requests", "safe": True}],
        "downloads": [{"name": "requests", "downloaded": False}],
    })
    assert module.main([str(project), "--download"]) == 1


def test_the_api_checks_without_saving_and_downloads_only_the_safe_file(monkeypatch):
    project = SAFE_ROOT / f"project-check-{uuid.uuid4().hex[:8]}"
    project.mkdir()
    (project / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
    payloads = {"https://pypi.org/pypi/requests/json": _pypi(), WHEEL_URL: WHEEL}
    calls, fetch = _fetch(payloads)
    monkeypatch.setattr(project_check, "fetch_url", fetch)
    client = TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))

    checked = client.post("/project-check", json={"path": str(project)}, headers=H)
    assert checked.status_code == 200, checked.text
    assert checked.json()["updates"][0]["safe"] is True
    assert WHEEL_URL not in calls
    assert not (project / ".setup-manager").exists()

    saved = client.post("/project-check/download", json={"path": str(project)}, headers=H)
    assert saved.status_code == 200, saved.text
    assert saved.json()["downloads"][0]["downloaded"] is True
    assert WHEEL_URL in calls
    target = project / ".setup-manager" / "updates" / "python" / "requests-2.32.3-py3-none-any.whl"
    assert target.read_bytes() == WHEEL


def test_a_path_outside_the_projects_root_is_refused(tmp_path):
    client = TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    response = client.post("/project-check", json={"path": str(tmp_path)}, headers=H)
    assert response.status_code == 400


def test_the_dashboard_offers_the_check_and_the_safe_download():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8") + (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
    c = TestClient(webapp.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    served = c.get("/").text + c.get("/static/dashboard.js").text
    for marker in ("/project-check", "Check files and updates", "Download safe updates"):
        assert marker in html
        assert marker in served
