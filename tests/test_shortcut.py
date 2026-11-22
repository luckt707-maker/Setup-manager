"""Desktop shortcut creation tests."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from create_shortcut import shortcut_spec


def test_the_shortcut_uses_the_virtual_environment_python(tmp_path):
    """The shortcut must run the venv, or FastAPI will be missing."""
    root = tmp_path / "SetupManagerv2"
    pythonw = root / "venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.touch()

    spec = shortcut_spec(root)

    assert spec["target"] == str(pythonw)


def test_the_shortcut_falls_back_to_the_running_interpreter(tmp_path):
    """With no venv, the interpreter that ran this script is used."""
    root = tmp_path / "SetupManagerv2"
    root.mkdir()

    spec = shortcut_spec(root, python_exe=r"C:\Python315\python.exe")

    assert spec["target"] == r"C:\Python315\python.exe"


def test_the_shortcut_points_at_the_app_and_works_from_the_app_folder(tmp_path):
    """The dashboard is started from its own folder, so relative paths work."""
    root = tmp_path / "SetupManagerv2"
    root.mkdir()
    (root / "app.py").touch()

    spec = shortcut_spec(root, python_exe="python.exe")

    assert spec["arguments"].startswith(f'"{root / "app.py"}"')
    assert spec["working_directory"] == str(root)


def test_the_shortcut_opens_the_dashboard_and_allows_the_projects_root(tmp_path, monkeypatch):
    """A double-click uses the Projects folder, not the folder the zip was unpacked in."""
    from core.settings import settings_manager

    settings_manager.set("paths", "safe_root", "")
    monkeypatch.delenv("SETUP_MANAGER_SAFE_ROOT", raising=False)
    projects = tmp_path / "Projects"
    monkeypatch.setattr("core.settings.default_projects_folder", lambda: projects)
    root = tmp_path / "Downloads" / "SetupManagerv2"
    root.mkdir(parents=True)

    spec = shortcut_spec(root, python_exe="python.exe")

    assert "--auto-open" in spec["arguments"]
    assert f'--safe-root "{projects.resolve()}"' in spec["arguments"]
    assert "Downloads" not in spec["arguments"].split("--safe-root", 1)[-1]


def test_an_explicit_safe_root_wins(tmp_path, monkeypatch):
    """A configured safe root must not be overwritten by the shortcut."""
    from core.settings import settings_manager

    settings_manager.set("paths", "safe_root", "")
    chosen = tmp_path / "other"
    chosen.mkdir()
    monkeypatch.setenv("SETUP_MANAGER_SAFE_ROOT", str(chosen))
    root = tmp_path / "SetupManagerv2"
    root.mkdir()

    spec = shortcut_spec(root, python_exe="python.exe")

    assert f'--safe-root "{chosen.resolve()}"' in spec["arguments"]


def test_the_shortcut_needs_no_environment_block(tmp_path):
    """pythonw.exe gets no environment from Explorer, so nothing may rely on it."""
    root = tmp_path / "SetupManagerv2"
    root.mkdir()

    spec = shortcut_spec(root, python_exe="python.exe")

    assert "environment" not in spec


def test_the_shortcut_describes_itself(tmp_path):
    """Windows shows the description in the shortcut's tooltip."""
    root = tmp_path / "SetupManagerv2"
    root.mkdir()

    spec = shortcut_spec(root, python_exe="python.exe")

    assert spec["description"]
    assert spec["icon"].endswith(", 0")
