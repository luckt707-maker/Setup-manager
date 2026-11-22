"""A broken settings file must not stop Setup Manager from opening."""

import os
import subprocess
import sys
from pathlib import Path

from core.settings import SettingsManager


def test_a_settings_file_that_is_not_an_object_uses_the_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("null", encoding="utf-8")

    manager = SettingsManager(path)

    assert manager.get("build", "build_timeout") == 300
    # The broken file is left as the user saved it.
    assert path.read_text(encoding="utf-8") == "null"


def test_a_settings_category_that_is_not_a_group_does_not_crash(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"build": "yes"}', encoding="utf-8")

    manager = SettingsManager(path)

    assert manager.get("build", "build_timeout", 300) == 300
    manager.set("build", "build_timeout", 120)
    assert manager.get("build", "build_timeout") == 120


def test_the_smoke_script_does_not_quit_just_because_it_was_imported(monkeypatch):
    """Running the file checks a live server. Importing it must not."""
    import importlib

    monkeypatch.delenv("SETUP_MANAGER_TOKEN", raising=False)
    import scripts.smoke_test as smoke

    importlib.reload(smoke)
    assert smoke.main() == 1


def test_importing_the_app_survives_a_blank_or_nonsense_port(tmp_path):
    """run.local.bat can set SETUP_MANAGER_PORT to nothing. That must not traceback."""
    env = os.environ.copy()
    env["SETUP_MANAGER_SETTINGS_FILE"] = str(tmp_path / "settings.json")
    env["SETUP_MANAGER_TASKS_DB"] = str(tmp_path / "tasks.db")
    env["SETUP_MANAGER_SAFE_ROOT"] = str(tmp_path)
    root = Path(__file__).resolve().parent.parent
    for value in ("", "abc", "0", "70000"):
        env["SETUP_MANAGER_PORT"] = value
        result = subprocess.run(
            [sys.executable, "-c", "import app; print(app.APP_PORT)"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "8010"
