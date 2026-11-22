"""User settings management for Setup Manager.

Provides persistent storage for build, execution, and deployment preferences.
Settings are stored in a JSON file in the logs directory.
"""
import copy
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("setup_manager.settings")

# Default settings
DEFAULT_SETTINGS: Dict[str, Any] = {
    "build": {
        "auto_install_deps": True,
        "parallel_builds": True,
        "build_timeout": 300,
        "clean_before_build": False,
    },
    "execution": {
        "auto_open_browser": True,
        "notify_on_complete": True,
        "log_level": "INFO",
        "max_concurrent_tasks": 4,
    },
    "deployment": {
        "docker_build_on_deploy": True,
        "docker_tag_latest": True,
        "health_check_enabled": False,
        "health_check_url": "/health",
    },
    "ui": {
        "theme": "auto",
        "show_workflow": True,
        "collapsible_actions": True,
    },
    # Set from the dashboard. Empty means "use SETUP_MANAGER_SAFE_ROOT".
    "paths": {
        "safe_root": "",
        "last_project": "",
    },
}


class SettingsManager:
    """Manages user settings persistence."""

    def __init__(self, settings_path: Optional[Path] = None):
        """Initialize settings manager.

        Args:
            settings_path: Path to settings.json file. Defaults to logs/settings.json.
        """
        if settings_path is None and os.getenv("SETUP_MANAGER_SETTINGS_FILE"):
            settings_path = Path(os.environ["SETUP_MANAGER_SETTINGS_FILE"])
        if settings_path is None:
            log_dir = Path(__file__).parent.parent / "logs"
            log_dir.mkdir(exist_ok=True)
            self.settings_path = log_dir / "settings.json"
        else:
            self.settings_path = settings_path

        self._settings: Dict[str, Any] = {}
        self.load()

    def load(self) -> Dict[str, Any]:
        """Load settings from file, merging with defaults."""
        try:
            if self.settings_path.exists():
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    user_settings = json.load(f)
                if not isinstance(user_settings, dict):
                    # "null", a list, or a single word is valid JSON and still
                    # not a settings file. Keep the file and use the defaults.
                    raise ValueError("settings file is not an object")
                # Deep merge with defaults
                self._settings = self._deep_merge(DEFAULT_SETTINGS, user_settings)
                logger.info("Loaded settings from %s", self.settings_path)
            else:
                self._settings = copy.deepcopy(DEFAULT_SETTINGS)
                self.save()
                logger.info("Created default settings at %s", self.settings_path)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            logger.error("Failed to load settings: %s. Using defaults.", e)
            self._settings = copy.deepcopy(DEFAULT_SETTINGS)
        return self._settings

    def save(self) -> None:
        """Save current settings to file."""
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
            logger.debug("Settings saved to %s", self.settings_path)
        except OSError as e:
            logger.error("Failed to save settings: %s", e)

    def get(self, category: str, key: Optional[str] = None, default: Any = None) -> Any:
        """Get a setting value.

        Args:
            category: Settings category (e.g., 'build', 'execution', 'deployment').
            key: Specific key within the category. If None, returns entire category.
            default: Default value if key doesn't exist.

        Returns:
            The setting value or default.
        """
        category_data = self._settings.get(category, {})
        if not isinstance(category_data, dict):
            # A hand-edited file can put a word where a group of settings
            # belongs. Treat that group as missing instead of crashing.
            return {} if key is None else default
        if key is None:
            return category_data
        return category_data.get(key, default)

    def set(self, category: str, key: str, value: Any) -> None:
        """Set a setting value and persist to file.

        Args:
            category: Settings category.
            key: Key within the category.
            value: Value to set.
        """
        self._ensure_category(category)[key] = value
        self.save()

    def update_category(self, category: str, updates: Dict[str, Any]) -> None:
        """Update multiple settings in a category at once.

        Args:
            category: Settings category.
            updates: Dictionary of key-value pairs to update.
        """
        self._ensure_category(category).update(updates)
        self.save()

    def reset(self, category: Optional[str] = None) -> None:
        """Reset settings to defaults.

        Args:
            category: If provided, reset only this category. Otherwise reset all.
        """
        if category:
            if category in DEFAULT_SETTINGS:
                self._settings[category] = dict(DEFAULT_SETTINGS[category])
        else:
            self._settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.save()
        logger.info("Settings reset to defaults")

    def _ensure_category(self, category: str) -> Dict[str, Any]:
        """The group of settings for ``category``, repaired if it was damaged."""
        current = self._settings.get(category)
        if isinstance(current, dict):
            return current
        repaired = dict(DEFAULT_SETTINGS.get(category) or {})
        self._settings[category] = repaired
        return repaired

    def all_settings(self) -> Dict[str, Any]:
        """Return all current settings."""
        return dict(self._settings)

    @staticmethod
    def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Deep merge two dictionaries.

        Args:
            base: Base dictionary.
            override: Override dictionary.

        Returns:
            Merged dictionary.
        """
        result = copy.deepcopy(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = SettingsManager._deep_merge(result[key], value)
            else:
                result[key] = value
        return result


# Global settings instance
settings_manager = SettingsManager()


# Notes from the last time the projects folder was chosen, so the dashboard
# can show them after the console window has already moved on.
_root_notes: list = []
# The "Created …" sentence is true only on the call that makes the folder.
# Startup does that before the page loads, so the sentence is remembered
# until the dashboard asks, and only while that same folder is in use.
_created_note = ""
_created_for = ""


def default_projects_folder() -> Path:
    """Where projects go when nobody has chosen a folder."""
    return Path.home() / "Projects"


def _folded(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _same_path(left: Path, right: Path) -> bool:
    return _folded(left) == _folded(right)


def _is_inside(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or a folder inside it."""
    if _same_path(path, root):
        return True
    return _folded(path).startswith(_folded(root) + os.sep)


def root_problem(raw: str) -> Optional[str]:
    """Why ``raw`` cannot be the projects folder, or None when it is allowed.

    The same rules apply to the dashboard, the shortcut and run.bat.
    A missing folder is a problem, except the built-in Projects folder,
    which is created the first time it is needed.
    """
    if not raw or not str(raw).strip():
        return "A project folder path is required."
    if "\x00" in str(raw):
        return "That path is not valid."
    try:
        candidate = Path(str(raw).strip()).expanduser()
    except (OSError, ValueError):
        return "That path is not valid."
    if not candidate.is_absolute():
        return "Use a full path, for example C:\\Users\\You\\Projects"
    try:
        resolved = candidate.resolve()
    except OSError:
        return "That path could not be read."

    default = default_projects_folder()
    try:
        default_resolved = default.resolve()
    except OSError:
        default_resolved = default
    missing_ok = _same_path(resolved, default_resolved)
    if not resolved.is_dir() and not missing_ok:
        return f"Folder does not exist: {resolved}"
    if resolved.parent == resolved:
        return "A whole drive cannot be the projects root; pick a folder on it."

    home = Path.home().resolve()
    if _same_path(resolved, home):
        return (
            f"{resolved} cannot be the projects folder. "
            "That is your whole home folder. Use a folder inside it, such as Projects."
        )

    # Downloads and Desktop are where a zip usually gets unpacked. Using
    # either one would let Setup Manager open every file that landed there.
    for name in ("Downloads", "Desktop"):
        unpack = home / name
        try:
            unpack = unpack.resolve()
        except OSError:
            continue
        if _same_path(resolved, unpack):
            return (
                f"{resolved} cannot be the projects folder. "
                "That is where downloads and unpacked zip files land. "
                "Use a folder such as Projects."
            )

    blocked = []
    if sys.platform == "win32":
        blocked.append(Path(os.environ.get("SystemRoot", r"C:\Windows")))
        blocked.extend(
            Path(os.environ[name])
            for name in ("ProgramFiles", "ProgramFiles(x86)")
            if os.environ.get(name)
        )
    else:
        blocked.extend(Path(item) for item in ("/etc", "/usr", "/bin", "/sbin", "/var", "/boot", "/root"))
    for item in blocked:
        try:
            item = item.resolve()
        except OSError:
            continue
        if _is_inside(resolved, item):
            return f"{resolved} cannot be the projects root"
    return None


def _configured_roots() -> list:
    """The folders someone asked for, most specific first.

    The dashboard choice wins, then SETUP_MANAGER_SAFE_ROOT. The folder
    the program was unpacked into is never on this list.
    """
    choices = []
    dashboard = settings_manager.get("paths", "safe_root") or ""
    if str(dashboard).strip():
        choices.append(("The folder chosen in the dashboard", str(dashboard).strip()))
    env = (os.getenv("SETUP_MANAGER_SAFE_ROOT") or "").strip()
    if env:
        choices.append(("SETUP_MANAGER_SAFE_ROOT", env))
    return choices


def ensure_projects_root() -> Path:
    """The projects folder every entry point should use.

    An invalid choice is skipped, with a sentence kept for the user.
    When nothing valid is set, the Projects folder in the home directory
    is created if it is not there yet.
    """
    global _root_notes, _created_note, _created_for
    notes = []
    chosen = None
    for label, raw in _configured_roots():
        problem = root_problem(raw)
        if problem:
            notes.append(f"{label} is set to {raw}. {problem}")
            continue
        try:
            chosen = Path(raw).expanduser().resolve()
        except OSError:
            notes.append(f"{label} is set to {raw}. That path could not be read.")
            continue
        break

    if chosen is None:
        try:
            chosen = default_projects_folder().expanduser().resolve()
        except OSError:
            chosen = default_projects_folder().expanduser()
        if notes:
            notes.append(f"Using {chosen} instead.")
            notes.append(
                "To pick a different folder, set SETUP_MANAGER_SAFE_ROOT in run.local.bat, "
                "or click Set as projects root in the dashboard."
            )

    if not chosen.is_dir():
        try:
            chosen.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            notes.append(
                f"Could not create {chosen}: {exc}. "
                "Create that folder yourself, or set SETUP_MANAGER_SAFE_ROOT in run.local.bat."
            )
        else:
            _created_for = _folded(chosen)
            _created_note = (
                f"Created {chosen} for your projects. "
                "To use a different folder, set SETUP_MANAGER_SAFE_ROOT in run.local.bat, "
                "or click Set as projects root in the dashboard."
            )
            notes.append(_created_note)
    if _created_note and _created_for == _folded(chosen) and _created_note not in notes:
        notes.insert(0, _created_note)
    _root_notes = notes
    return chosen


def projects_root_notes() -> list:
    """Sentences from the last projects-folder choice. Empty when it was fine."""
    return list(_root_notes)


def projects_root() -> Path:
    """The main projects folder, and the only folder Docker may mount."""
    return ensure_projects_root()


def allowed_project_roots() -> list:
    """Folders a project path may live in.

    The folder in use is the main one. A second folder stays allowed when
    SETUP_MANAGER_SAFE_ROOT points somewhere else and that place is safe,
    so tools kept in a second folder can still be opened. An unsafe value
    is not added.
    """
    roots = [projects_root()]
    extra = (os.getenv("SETUP_MANAGER_SAFE_ROOT") or "").strip()
    if not extra or root_problem(extra):
        return roots
    try:
        path = Path(extra).expanduser().resolve()
    except OSError:
        return roots
    if path.is_dir() and path not in roots:
        roots.append(path)
    return roots
