from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple, Union
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time


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


class CommandExecutor:
    """Execute system commands safely.

    Cancellation: if ``callback`` has a ``cancel_event`` attribute (a threading.Event),
    setting it stops the running command and its child processes.
    """

    IMAGE_MAPPING = {
        "python": "python:3.11-slim",
        "nodejs": "node:20-slim",
        "dotnet": "mcr.microsoft.com/dotnet/sdk:8.0",
        "general": "ubuntu:latest"
    }

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
        try:
            if isinstance(command, str):
                command_args = command if allow_shell else shlex.split(command)
            else:
                command_args = list(command)
            if not command_args:
                raise ValueError("Command cannot be empty")

            # --- Docker Isolation Wrapping ---
            if run_mode == "docker":
                if not image:
                    # Try to infer image from project type if possible,
                    # otherwise fallback to general
                    image = CommandExecutor.IMAGE_MAPPING.get("general")

                # Construct Docker command: docker run --rm -v host:container -w /app image command
                docker_cmd = [
                    "docker", "run", "--rm",
                    "-v", f"{cwd}:/app",
                    "-w", "/app",
                    image
                ] + command_args
                command_args = docker_cmd
                allow_shell = False # Force shell=False for docker run

            command_text = " ".join(command_args) if isinstance(command_args, list) else str(command_args)
            message = f"\n🔧 Running: {command_text}"
            if callback:
                callback(message)
            else:
                print(message)

            popen_flags: Dict[str, Any] = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt" else {"start_new_session": True}
            )
            running_process = subprocess.Popen(
                command_args,
                cwd=cwd,
                shell=allow_shell and isinstance(command, str),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **popen_flags,
            )
            process = running_process

            reader_errors = []

            def stream_output() -> None:
                if running_process.stdout is None:
                    return
                for line in running_process.stdout:
                    rendered = line.rstrip()
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
                        msg = "🛑 Cancelled by user."
                        callback(msg) if callback else print(msg)
                        return False, None
                    if deadline is not None and time.monotonic() > deadline:
                        _kill_process_tree(running_process)
                        running_process.wait()
                        reader.join(timeout=5)
                        msg = f"⏱️ Command timed out after {timeout} seconds and was killed."
                        callback(msg) if callback else print(msg)
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
                if callback:
                    callback(msg)
                else:
                    print(msg)
                return False, result_code

            if callback:
                callback("✅ Success")
            else:
                print("✅ Success")
            return True, 0
        except FileNotFoundError:
            msg = "❌ Command not found. Install it or choose Install QoL Extensions first."
            if callback:
                callback(msg)
            else:
                print(msg)
            return False, None
        except Exception as e:
            if process is not None and process.poll() is None:
                _kill_process_tree(process)
                process.wait()
            msg = f"❌ Error executing command: {e}"
            if callback:
                callback(msg)
            else:
                print(msg)
            return False, None

    @staticmethod
    def check_installed(command: Union[str, Sequence[str]]) -> bool:
        """Check if a command is available"""
        if isinstance(command, Sequence) and not isinstance(command, (str, bytes)):
            command_seq = list(command)
            return bool(command_seq) and shutil.which(str(command_seq[0])) is not None

        if not isinstance(command, str):
            return False

        try:
            command_name = shlex.split(command, posix=False)[0]
        except (IndexError, ValueError):
            return False
        return shutil.which(command_name.strip('"')) is not None
