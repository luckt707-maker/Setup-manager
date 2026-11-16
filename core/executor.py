from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union
import locale
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from core.cmdline import split_command
from core.settings import projects_root


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Kill a process and everything it spawned (npm -> node, pip -> build backends, ...)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, check=False, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def decode_output(raw: bytes) -> str:
    """Decode a line of output from a child process.

    Tools disagree about encoding: Python and Node write UTF-8, while a
    Windows console program writes the active code page. UTF-8 is tried
    first because it never fails on plain ASCII and it is what most
    tools use; the code page is the fallback.
    """
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        fallback = locale.getpreferredencoding(False) or "cp1252"
        return raw.decode(fallback, errors="replace")


# Official images for the tools the launcher actually starts. Anything else
# gets a plain Ubuntu image, which is said out loud in the log.
_IMAGE_BY_TOOL = {
    "python": "python",
    "python3": "python",
    "py": "python",
    "pip": "python",
    "pip3": "python",
    "node": "nodejs",
    "npm": "nodejs",
    "npx": "nodejs",
    "pnpm": "nodejs",
    "yarn": "nodejs",
    "dotnet": "dotnet",
    "go": "go",
    "cargo": "rust",
    "rustc": "rust",
}

_IMAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,200}\Z")
_SCRIPT_IN_SHIM = re.compile(r'"([^"\r\n]+\.(?:js|cjs))"', re.IGNORECASE)
_NODE_SHIM_SCRIPTS = {
    "npm": ("node_modules/npm/bin/npm-cli.js", "node_modules/corepack/dist/npm.js"),
    "npx": ("node_modules/npm/bin/npx-cli.js", "node_modules/corepack/dist/npx.js"),
    "pnpm": ("node_modules/pnpm/bin/pnpm.cjs", "node_modules/corepack/dist/pnpm.js"),
    "yarn": ("node_modules/yarn/bin/yarn.js", "node_modules/corepack/dist/yarn.js"),
}


def _tool_name(argv: Sequence[str]) -> str:
    name = Path(str(argv[0])).name.lower()
    for ext in (".cmd", ".bat", ".exe"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _is_inside(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or a folder inside it.

    ``Path.relative_to`` is case-sensitive on some Python builds, and a
    Windows path can come back with a different drive-letter case than the
    root that was saved in settings. Compare the folded paths as well.
    """
    path = path.resolve()
    root = root.resolve()
    try:
        path.relative_to(root)
        return True
    except ValueError:
        folded_path = os.path.normcase(str(path))
        folded_root = os.path.normcase(str(root))
        return folded_path == folded_root or folded_path.startswith(folded_root + os.sep)


def docker_bind_source(path: Path) -> str:
    """Host path for ``docker run -v``, in the form Docker's parser accepts.

    ``E:\\work\\app:/app`` is split on the drive-letter colon, so Docker
    mounts ``E`` and treats the rest as the container path. Forward slashes
    keep the drive letter (``E:/work/app:/app``) and the parser rejoins it.
    """
    resolved = Path(path).resolve()
    if os.name == "nt":
        return str(resolved).replace("\\", "/")
    return str(resolved)


def image_for_command(argv: Sequence[str]) -> str:
    """The container image for this command, taken from the program it starts."""
    key = _IMAGE_BY_TOOL.get(_tool_name(argv), "general")
    return CommandExecutor.IMAGE_MAPPING[key]


def _container_command(argv: Sequence[str]) -> List[str]:
    """The same command, named the way the container's PATH expects it."""
    return [_tool_name(argv), *[str(part) for part in argv[1:]]]


def build_docker_invocation(argv: Sequence[str], cwd: str, image: Optional[str] = None) -> List[str]:
    """``docker run`` that mounts the project and refuses anything outside it."""
    if not cwd or not str(cwd).strip():
        raise ValueError("Docker mode needs a project folder to mount")
    project = Path(cwd).expanduser().resolve()
    root = projects_root()
    if not _is_inside(project, root):
        raise PermissionError(
            f"Refusing to mount {project} into Docker: it is outside the projects root {root}"
        )
    if not project.is_dir():
        raise NotADirectoryError(f"Docker mode needs a project folder, not {project}")
    chosen = image or image_for_command(argv)
    if not _IMAGE_NAME.fullmatch(chosen or ""):
        raise ValueError(f"Refusing Docker image {chosen!r}")
    return [
        "docker", "run", "--rm",
        "-v", f"{docker_bind_source(project)}:/app",
        "-w", "/app",
        chosen,
        *_container_command(argv),
    ]


def _expand_shim_path(raw: str, shim_dir: str) -> str:
    expanded = raw.replace("%~dp0", shim_dir + "\\").replace("%dp0%", shim_dir + "\\")
    if "%" in expanded:
        return ""
    return os.path.normpath(expanded)


def _node_cli_argv(shim: str, args: Sequence[str]) -> Optional[List[str]]:
    """Run a Node shim (npm.cmd, npx.cmd, ...) as ``node.exe script.js``.

    CreateProcess cannot start a ``.cmd`` file. The shim's own last line is
    ``node.exe`` plus a JavaScript entry point, which can be started directly
    and does not go back through cmd.exe.
    """
    shim_dir = os.path.dirname(shim)
    name = os.path.splitext(os.path.basename(shim))[0].lower()
    try:
        text = Path(shim).read_text(encoding="utf-8", errors="replace")[:65536]
    except OSError:
        text = ""
    scripts: List[str] = []
    for match in _SCRIPT_IN_SHIM.findall(text):
        if "prefix" in match.lower():
            continue
        expanded = _expand_shim_path(match, shim_dir)
        if expanded and os.path.isfile(expanded):
            scripts.append(expanded)
    for relative in _NODE_SHIM_SCRIPTS.get(name, ()):
        candidate = os.path.normpath(os.path.join(shim_dir, relative))
        if os.path.isfile(candidate) and candidate not in scripts:
            scripts.append(candidate)
    if not scripts:
        return None
    node = os.path.join(shim_dir, "node.exe")
    if not os.path.isfile(node):
        node = shutil.which("node") or ""
    if not node:
        return None
    return [node, scripts[0], *[str(part) for part in args]]


def _batch_path(head: str) -> Optional[str]:
    """The ``.cmd`` or ``.bat`` file ``head`` names, if CreateProcess cannot run it."""
    if os.name != "nt" or not head:
        return None
    ext = os.path.splitext(head)[1].lower()
    if ext in {".cmd", ".bat"} and os.path.isfile(head):
        return os.path.abspath(head)
    if ext:
        return None
    found = shutil.which(head)
    if found and os.path.splitext(found)[1].lower() in {".cmd", ".bat"}:
        return found
    return None


def _rewrite_gradle_wrapper(argv: List[str], cwd: Optional[str]) -> List[str]:
    """Use ``gradlew.bat`` where the shell script cannot be started."""
    if os.name != "nt" or not argv:
        return argv
    head = argv[0].replace("/", "\\").lower()
    if head not in {".\\gradlew", "gradlew", "gradlew.bat"}:
        return argv
    batch = Path(cwd or os.getcwd()) / "gradlew.bat"
    if batch.is_file():
        return [str(batch), *[str(part) for part in argv[1:]]]
    return argv


def _cmd_command_line(batch: str, args: Sequence[str]) -> str:
    """A command line that runs a batch file without ``shell=True``.

    The extra quotes are for ``cmd /s /c``: it strips one pair, and what
    remains quotes every argument, so ``&``, ``|`` and ``%`` stay data.
    """
    def quote(part: str) -> str:
        return '"' + str(part).replace("%", "%%").replace('"', '""') + '"'

    inner = " ".join(quote(part) for part in [batch, *args])
    cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    return f'{cmd} /d /s /c "{inner}"'


def prepare_host_command(argv: Sequence[str], cwd: Optional[str] = None) -> Union[List[str], str]:
    """Turn a command into something CreateProcess can start.

    On Windows, ``npm`` is ``npm.cmd``. Starting that name fails with
    "command not found" even though ``where npm`` finds it.
    """
    command = _rewrite_gradle_wrapper([str(part) for part in argv], cwd)
    batch = _batch_path(command[0]) if command else None
    if batch is None:
        return command
    node_argv = _node_cli_argv(batch, command[1:])
    if node_argv is not None:
        return node_argv
    return _cmd_command_line(batch, command[1:])


class CommandExecutor:
    """Execute system commands safely.

    Cancellation: if ``callback`` has a ``cancel_event`` attribute (a threading.Event),
    setting it stops the running command and its child processes.
    """

    IMAGE_MAPPING = {
        "python": "python:3.11-slim",
        "nodejs": "node:20-slim",
        "dotnet": "mcr.microsoft.com/dotnet/sdk:8.0",
        "go": "golang:1.23",
        "rust": "rust:1-slim",
        "general": "ubuntu:latest",
    }

    @staticmethod
    def image_for_command(argv: Sequence[str]) -> str:
        return image_for_command(argv)

    @staticmethod
    def run(
        command,
        cwd: Optional[str] = None,
        show_output: bool = True,
        allow_shell: bool = False,
        timeout: int = 600,
        callback: Optional[Callable[[str], None]] = None,
        run_mode: str = "host",
        image: Optional[str] = None,
        skip_exit_codes: Optional[Iterable[int]] = None,
    ) -> bool:
        """Execute a command and return success status"""
        success, _ = CommandExecutor._execute(
            command,
            cwd=cwd,
            show_output=show_output,
            allow_shell=allow_shell,
            timeout=timeout,
            callback=callback,
            run_mode=run_mode,
            image=image,
            skip_exit_codes=skip_exit_codes,
        )
        return success

    @staticmethod
    def run_with_code(command, **kwargs) -> Tuple[bool, Optional[int]]:
        """Execute a command and return ``(success, exit_code)``.

        The exit code is ``None`` when the command never ran to completion
        (cancelled, timed out, or could not be started at all). Callers that
        need to tell "failed" apart from "found nothing to do" - a test
        runner that collected no tests exits with 5 - need the code, not just
        a boolean.
        """
        return CommandExecutor._execute(command, **kwargs)

    @staticmethod
    def _execute(
        command,
        cwd: Optional[str] = None,
        show_output: bool = True,
        allow_shell: bool = False,
        timeout: int = 600,
        callback: Optional[Callable[[str], None]] = None,
        run_mode: str = "host",
        image: Optional[str] = None,
        skip_exit_codes: Optional[Iterable[int]] = None,
    ) -> Tuple[bool, Optional[int]]:
        """Execute a command; returns ``(success, exit_code)``.

        ``skip_exit_codes`` lists exit codes the caller considers "there was
        nothing to do" rather than a failure. They are reported with a
        neutral message instead of the usual error line.
        """
        skip_exit_codes = set(skip_exit_codes or ())
        process = None

        def _say(msg: str) -> None:
            if callback:
                callback(msg)
            else:
                print(msg)

        try:
            use_docker = str(run_mode).lower() == "docker"
            if isinstance(command, str):
                command_args: Union[List[str], str] = command if allow_shell and not use_docker else split_command(command)
            else:
                command_args = [str(part) for part in command]
            if not command_args:
                raise ValueError("Command cannot be empty")

            if use_docker and isinstance(command_args, list) and _tool_name(command_args) != "docker":
                command_args = build_docker_invocation(command_args, cwd or "", image)
            elif isinstance(command_args, list):
                if use_docker:
                    _say("Docker commands run on the host. They are not started inside another container.")
                command_args = prepare_host_command(command_args, cwd)

            command_text = " ".join(command_args) if isinstance(command_args, list) else command_args
            _say(f"\n🔧 Running: {command_text}")

            popen_flags: Dict[str, Any] = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt" else {"start_new_session": True}
            )
            child_env = os.environ.copy()
            # A Windows console uses cp1252. Python then crashes on an emoji
            # such as the one in Sherlock's welcome screen.
            child_env.setdefault("PYTHONUTF8", "1")
            child_env.setdefault("PYTHONIOENCODING", "utf-8")
            running_process = subprocess.Popen(
                command_args,
                cwd=cwd,
                env=child_env,
                shell=bool(allow_shell and isinstance(command, str) and isinstance(command_args, str)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                **popen_flags,
            )
            process = running_process

            reader_errors = []

            def stream_output() -> None:
                if running_process.stdout is None:
                    return
                for line in running_process.stdout:
                    rendered = decode_output(line).rstrip()
                    try:
                        if callback:
                            callback(rendered)
                        elif show_output:
                            print(rendered)
                    except Exception as exc:
                        reader_errors.append(exc)

            reader = threading.Thread(target=stream_output, daemon=True)
            reader.start()
            cancel_event = getattr(callback, "cancel_event", None)
            deadline = time.monotonic() + timeout if timeout else None
            result_code = None
            while result_code is None:
                try:
                    result_code = running_process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    if cancel_event is not None and cancel_event.is_set():
                        _kill_process_tree(running_process)
                        running_process.wait()
                        reader.join(timeout=5)
                        _say("🛑 Cancelled by user.")
                        return False, None
                    if deadline is not None and time.monotonic() > deadline:
                        _kill_process_tree(running_process)
                        running_process.wait()
                        reader.join(timeout=5)
                        _say(f"⏱️ Command timed out after {timeout} seconds and was killed.")
                        return False, None
            reader.join(timeout=5)
            if reader_errors:
                raise reader_errors[0]

            if result_code != 0:
                if result_code in skip_exit_codes:
                    # The caller told us this code means "there was nothing to
                    # do", not "the command failed".
                    msg = f"⚪ Command exited with code {result_code}"
                else:
                    msg = f"❌ Command failed with exit code {result_code}"
                _say(msg)
                return False, result_code

            _say("✅ Success")
            return True, 0
        except FileNotFoundError:
            msg = "❌ Command not found. Install it or choose Install QoL Extensions first."
            _say(msg)
            return False, None
        except (PermissionError, NotADirectoryError, ValueError) as exc:
            if process is not None and process.poll() is None:
                _kill_process_tree(process)
                process.wait()
            _say(f"❌ {exc}")
            return False, None
        except Exception as e:
            if process is not None and process.poll() is None:
                _kill_process_tree(process)
                process.wait()
            _say(f"❌ Error executing command: {e}")
            return False, None

    @staticmethod
    def check_installed(command: Union[str, Sequence[str]]) -> bool:
        """Check if a command is available"""
        if isinstance(command, Sequence) and not isinstance(command, (str, bytes)):
            command_seq = list(command)
            return bool(command_seq) and CommandExecutor.check_installed(str(command_seq[0]))

        if not isinstance(command, str):
            return False

        try:
            command_name = split_command(command)[0].strip('"')
        except (IndexError, ValueError):
            return False
        if not command_name:
            return False
        # A path is a file in the project, not a program on PATH. ``gradlew.bat``
        # is the same kind of thing: it lives next to the project, and PATH
        # will not find it.
        if os.path.isabs(command_name) or command_name.startswith(("./", ".\\")):
            return True
        if os.name == "nt" and command_name.lower() in {"gradlew", "gradlew.bat"}:
            return True
        return shutil.which(command_name) is not None
