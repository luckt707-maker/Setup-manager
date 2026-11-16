"""Windows command launching and Docker mode.

npm, pnpm, yarn and npx are batch files on Windows. CreateProcess cannot
start them, so a setup that "found" npm still failed. Docker mode used to
mount ``E:\\project`` by splitting on the drive-letter colon, and it picked
the image from whichever language was detected first.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.executor import (  # noqa: E402
    CommandExecutor,
    build_docker_invocation,
    prepare_host_command,
)
from core.settings import projects_root  # noqa: E402


class _Stopped:
    """A child that has already finished, so the executor never starts a real one."""

    def __init__(self):
        self.stdout = iter(())
        self.pid = 1
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def kill(self):
        pass


def _project(name: str) -> Path:
    folder = projects_root() / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_a_node_shim_runs_as_node_and_its_javascript_entry(tmp_path, monkeypatch):
    """npm.cmd is not an executable. The shim names the script node should run."""
    node = tmp_path / "node.exe"
    node.write_bytes(b"")
    script = tmp_path / "node_modules" / "npm" / "bin" / "npm-cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("// entry\n", encoding="utf-8")
    shim = tmp_path / "npm.cmd"
    shim.write_text(
        '@ECHO off\r\n"%~dp0\\node.exe" "%~dp0\\node_modules\\npm\\bin\\npm-cli.js" %*\r\n',
        encoding="ascii",
    )

    def which(name):
        if name == "npm":
            return str(shim)
        if name == "node":
            return str(node)
        return None

    monkeypatch.setattr("core.executor.shutil.which", which)

    assert prepare_host_command(["npm", "install", "--ignore-scripts"]) == [
        str(node),
        str(script),
        "install",
        "--ignore-scripts",
    ]


def test_a_batch_file_keeps_metacharacters_inside_one_argument(tmp_path):
    """``safe&echo PWNED`` must be one argument, not a second command."""
    if os.name != "nt":
        pytest.skip("cmd.exe quoting is a Windows behavior")
    batch = tmp_path / "echoarg.cmd"
    batch.write_text("@echo off\r\necho LINE=%*\r\n", encoding="ascii")
    lines = []

    ok = CommandExecutor.run([str(batch), "safe&echo PWNED"], callback=lines.append)

    rendered = "\n".join(lines)
    assert ok is True, rendered
    assert 'safe&echo PWNED' in rendered
    assert "\nPWNED" not in rendered


def test_python_is_not_sent_through_cmd(tmp_path):
    script = tmp_path / "hello.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    prepared = prepare_host_command([sys.executable, str(script)])
    assert isinstance(prepared, list)
    assert prepared[0] == sys.executable


def test_docker_mount_keeps_the_drive_letter(tmp_path):
    project = _project("docker-mount")
    invocation = build_docker_invocation(["python", "app.py"], str(project))
    mount = invocation[invocation.index("-v") + 1]
    assert mount.endswith(":/app")
    source = mount[: -len(":/app")]
    assert "\\" not in source
    if os.name == "nt":
        assert source[1:3] == ":/"
    assert invocation[invocation.index("-w") + 2] == "python:3.11-slim"


def test_docker_image_follows_the_command_being_run():
    project = _project("docker-image")
    node = build_docker_invocation(["npm", "start"], str(project))
    dotnet = build_docker_invocation(["dotnet", "run"], str(project))
    explicit = build_docker_invocation(["npm", "start"], str(project), image="python:3.11-slim")
    assert "node:20-slim" in node
    assert "mcr.microsoft.com/dotnet/sdk:8.0" in dotnet
    assert "python:3.11-slim" in explicit
    assert "node:20-slim" not in explicit


def test_docker_refuses_a_folder_outside_the_projects_root():
    with pytest.raises(PermissionError):
        build_docker_invocation(["python", "app.py"], r"C:\Windows")


def test_docker_refuses_an_image_name_that_is_not_an_image():
    project = _project("docker-image-name")
    with pytest.raises(ValueError):
        build_docker_invocation(["python", "app.py"], str(project), image="ubuntu; rm -rf /")


def test_docker_mode_does_not_wrap_the_docker_client(monkeypatch):
    """``docker compose`` needs the host engine, not a container that has no client."""
    project = _project("docker-client")
    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        return _Stopped()

    monkeypatch.setattr("core.executor.subprocess.Popen", fake_popen)
    ok = CommandExecutor.run(
        ["docker", "compose", "up", "web"],
        cwd=str(project),
        run_mode="docker",
        show_output=False,
    )
    assert ok is True
    assert seen["command"][:4] == ["docker", "compose", "up", "web"]


def test_docker_mode_mounts_the_project_and_does_not_start_when_refused(monkeypatch):
    project = _project("docker-run")
    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["shell"] = kwargs.get("shell")
        return _Stopped()

    monkeypatch.setattr("core.executor.subprocess.Popen", fake_popen)
    lines = []
    ok = CommandExecutor.run(
        ["npm", "start"], cwd=str(project), run_mode="docker", callback=lines.append
    )
    assert ok is True, lines
    assert seen["shell"] is False
    command = seen["command"]
    assert command[:4] == ["docker", "run", "--rm", "-v"]
    assert command[5:7] == ["-w", "/app"]
    assert "node:20-slim" in command

    seen.clear()
    refused = CommandExecutor.run(
        ["npm", "start"], cwd=r"C:\Windows", run_mode="docker", callback=lines.append
    )
    assert refused is False
    assert "command" not in seen
    assert any("outside the projects root" in line for line in lines)


def test_gradle_wrapper_is_rewritten_to_the_batch_file(tmp_path):
    if os.name != "nt":
        pytest.skip("gradlew.bat is the Windows wrapper")
    (tmp_path / "gradlew.bat").write_text("@echo off\r\n", encoding="ascii")
    prepared = prepare_host_command(["./gradlew", "build"], cwd=str(tmp_path))
    assert isinstance(prepared, str)
    assert "gradlew.bat" in prepared
    assert "build" in prepared
