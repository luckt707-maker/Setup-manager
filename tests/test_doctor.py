"""Tests for the doctor: knowledge base, diagnostics, repairs and history."""
import json
import os
from pathlib import Path

import pytest

from doctor import (
    BuildDoctor,
    DoctorLogManager,
    KnowledgeBase,
    available_repairs,
    check_tooling,
    check_wsl_config,
    check_zone_identifier,
    diagnose_build_output,
    run_machine_diagnostics,
    summarize,
)
from doctor.repairs import clear_pycache, prune_broken_symlinks, strip_zone_identifier


# --------------------------------------------------------------------------- #
# Knowledge base
# --------------------------------------------------------------------------- #
def test_known_failures_are_matched():
    assert KnowledgeBase.lookup("pattern client/dist/*: no matching files found")["name"] == "Go embed has nothing to include"
    assert KnowledgeBase.lookup("the project wants python 2.7")["name"] == "Legacy Python Prerequisite"
    assert KnowledgeBase.lookup("No module named 'requests'")["name"] == "Missing Python Package"
    assert KnowledgeBase.lookup("npm ERR! code E404")["name"] == "Node Dependency Failure"
    assert KnowledgeBase.lookup("CMake Error at configure time")["name"] == "CMake Configuration Problem"


def test_specific_signature_beats_the_general_one():
    found = KnowledgeBase.lookup("Too many levels of symbolic links")
    assert found["name"] == "Self Aware Build System"


def test_unknown_text_does_not_match():
    assert KnowledgeBase.lookup("something entirely different") is None
    assert KnowledgeBase.lookup("") is None
    assert KnowledgeBase.lookup(None) is None


def test_extra_knowledge_is_loaded_from_the_knowledge_directory():
    """The JSON files in knowledge/ extend the built-in table."""
    assert KnowledgeBase.extra_failures(), "no extra knowledge was loaded"
    assert "EADDRINUSE" in KnowledgeBase.extra_failures()
    assert KnowledgeBase.lookup("EADDRINUSE: address already in use")["name"] == "Port already in use"


def test_iter_matches_finds_several_problems_at_once():
    matches = dict(KnowledgeBase.iter_matches("EADDRINUSE and also gyp ERR! build failure"))
    assert "EADDRINUSE" in matches
    assert "gyp ERR!" in matches


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def test_tooling_check_reports_every_expected_tool():
    findings = check_tooling()
    assert {f.id for f in findings} == {
        "tool.python", "tool.node", "tool.npm", "tool.git", "tool.docker",
        "tool.make",
    }
    for finding in findings:
        assert finding.status in ("ok", "warn")


def test_python_project_does_not_require_unused_tools(tmp_path):
    """A Dockerfile, a stub go.mod, and an empty package.json are not toolchains."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module myproject\n\ngo 1.21\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"dependencies":{"left-pad":"1.0.0"}}\n', encoding="utf-8")
    from doctor.diagnostics import required_tools

    needed = required_tools(tmp_path)
    assert "python" in needed
    assert "node" not in needed
    assert "npm" not in needed
    assert "docker" not in needed
    assert "make" not in needed
    assert "go" not in needed
    findings = {item.id: item for item in check_tooling(tmp_path)}
    assert findings["tool.node"].status == "ok"
    assert findings["tool.docker"].status == "ok"
    assert findings["tool.make"].status == "ok"


def test_a_nested_web_client_does_not_make_the_parent_a_node_app(tmp_path):
    """PhoneInfoga's Vue client lives under web/client. The Go program does not need npm."""
    from doctor.diagnostics import required_tools

    root = tmp_path / "PhoneInfoga"
    client = root / "web" / "client" / "src"
    client.mkdir(parents=True)
    (root / "go.mod").write_text("module example.com/phoneinfoga\n\ngo 1.20\n", encoding="utf-8")
    (root / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    (root / "package.json").write_text('{"dependencies":{"build":"0.1.4","main":"1000.0.1"}}\n', encoding="utf-8")
    (root / "web" / "client" / "package.json").write_text(
        '{"scripts":{"build":"vue-cli-service build"}}\n', encoding="utf-8"
    )
    (client / "main.ts").write_text("export {}\n", encoding="utf-8")

    needed = required_tools(root)
    assert "go" in needed
    assert "node" not in needed
    assert "npm" not in needed
    client_needed = required_tools(root / "web" / "client")
    assert "node" in client_needed


def test_real_manifests_require_their_tools(tmp_path):
    from doctor.diagnostics import required_tools

    node = tmp_path / "node-app"
    node.mkdir()
    (node / "package.json").write_text(
        '{"scripts":{"test":"node test.js"}}\n', encoding="utf-8"
    )
    needed = required_tools(node)
    assert needed["node"]
    assert needed["npm"]
    assert "docker" not in needed

    docker_only = tmp_path / "image"
    docker_only.mkdir()
    (docker_only / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    assert "docker" in required_tools(docker_only)

    built = tmp_path / "built"
    built.mkdir()
    (built / "Makefile").write_text("all:\n\t@echo hi\n", encoding="utf-8")
    (built / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    (built / "go.mod").write_text("module example.com/built\n\ngo 1.21\n", encoding="utf-8")
    go_needed = required_tools(built)
    assert "make" in go_needed
    assert "go" in go_needed


def test_machine_diagnostics_run_and_summarise():
    findings = run_machine_diagnostics()
    counts = summarize(findings)
    assert counts["ok"] + counts["warn"] + counts["fail"] == len(findings)


def test_zone_identifier_check_finds_nothing_in_a_clean_tree(tmp_path):
    findings = check_zone_identifier(tmp_path)
    assert findings[0].status == "ok"


def test_wsl_config_check_accepts_a_valid_file(tmp_path):
    target = tmp_path / "wsl.conf"
    target.write_text("[automount]\nenabled = true\n", encoding="utf-8")
    findings = check_wsl_config(target)
    assert findings[0].status == "ok"


def test_wsl_config_check_rejects_a_missing_equals(tmp_path):
    target = tmp_path / "wsl.conf"
    target.write_text("[automount]\nenabled true\n", encoding="utf-8")
    findings = check_wsl_config(target)
    assert findings[0].status == "fail"
    assert "line 2" in findings[0].detail


def test_build_output_is_diagnosed():
    findings = diagnose_build_output("CMake Error: could not find LIBC")
    matched = [f for f in findings if f.id.startswith("build.")]
    assert matched
    assert matched[0].fix


def test_unknown_build_output_is_reported_as_unknown():
    findings = diagnose_build_output("something nobody has seen before")
    assert any(f.id == "build.unknown" for f in findings)


# --------------------------------------------------------------------------- #
# Repairs
# --------------------------------------------------------------------------- #
def test_strip_zone_identifier_removes_the_files(tmp_path):
    stray = tmp_path / "Zone.Identifier"
    stray.write_text("metadata", encoding="utf-8")
    changed, message = strip_zone_identifier(tmp_path).apply()
    assert changed == 1
    assert not stray.exists()
    assert "1" in message


@pytest.mark.skipif(os.name == "nt", reason="a colon is not allowed in a Windows filename")
def test_strip_zone_identifier_removes_the_ads_style_name(tmp_path):
    """The usual form is 'archive.zip:Zone.Identifier', which Linux and WSL allow."""
    stray = tmp_path / "archive.zip:Zone.Identifier"
    stray.write_text("metadata", encoding="utf-8")
    changed, _ = strip_zone_identifier(tmp_path).apply()
    assert changed == 1
    assert not stray.exists()


def test_prune_broken_symlinks_removes_only_broken_ones(tmp_path):
    good = tmp_path / "good.txt"
    good.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(tmp_path / "does-not-exist")
    except (OSError, NotImplementedError):
        return  # symlinks are not always available on Windows
    changed, _ = prune_broken_symlinks(tmp_path).apply()
    assert changed == 1
    assert good.exists()
    assert not link.exists()


def test_clear_pycache_removes_cached_bytecode(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-312.pyc").write_bytes(b"\x00")
    changed, _ = clear_pycache(tmp_path).apply()
    assert changed == 1


def test_repairs_are_offered_for_the_findings_that_match(tmp_path):
    stray = tmp_path / "Zone.Identifier"
    stray.write_text("metadata", encoding="utf-8")
    findings = check_zone_identifier(tmp_path)
    repairs = available_repairs(findings, tmp_path)
    assert any(r.id == "strip_zone_identifier" for r in repairs)


def test_dry_run_changes_nothing(tmp_path):
    stray = tmp_path / "Zone.Identifier"
    stray.write_text("metadata", encoding="utf-8")

    from doctor.repairs import apply_repair

    changed, message = apply_repair(strip_zone_identifier(tmp_path), dry_run=True)
    assert changed is False
    assert "dry run" in message
    assert stray.exists(), "a dry run must not delete anything"


# --------------------------------------------------------------------------- #
# History and reports
# --------------------------------------------------------------------------- #
def test_log_manager_records_and_returns_history(tmp_path):
    manager = DoctorLogManager(tmp_path)
    manager.record({"summary": {"ok": 1}})
    manager.record({"summary": {"ok": 2}})
    history = manager.history()
    assert len(history) == 2
    assert history[0]["summary"]["ok"] == 2  # newest first
    assert "timestamp" in history[0]


def test_log_manager_clear_removes_the_history(tmp_path):
    manager = DoctorLogManager(tmp_path)
    manager.record({"summary": {}})
    assert manager.clear() == 1
    assert manager.history() == []


def test_build_doctor_writes_a_report(tmp_path):
    doctor = BuildDoctor(tmp_path, log_manager=DoctorLogManager(tmp_path / "logs"), report_dir=tmp_path / "reports")
    result = doctor.run()
    assert result["summary"]["ok"] >= 1
    report = doctor.write_report(result)
    assert report is not None and report.exists()
    assert json.loads(report.read_text(encoding="utf-8"))["summary"] == result["summary"]


def test_the_doctor_pass_covers_the_project_dependencies(tmp_path):
    """One pass over a folder should say what the folder is missing."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "requirements.txt").write_text("requests\n", encoding="utf-8")

    doctor = BuildDoctor(project, log_manager=DoctorLogManager(tmp_path / "logs"), report_dir=tmp_path / "reports")
    result = doctor.run()

    ids = [finding["id"] for finding in result["diagnostics"]]
    assert "dependencies.not_installed" in ids, result["diagnostics"]
