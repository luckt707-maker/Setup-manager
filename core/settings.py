"""User settings management for Setup Manager.

Provides persistent storage for build, execution, and deployment preferences.
Settings are stored in a JSON file in the logs directory.
"""
import copy
import json
import logging
import os
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
                # Deep merge with defaults
                self._settings = self._deep_merge(DEFAULT_SETTINGS, user_settings)
                logger.info("Loaded settings from %s", self.settings_path)
            else:
                self._settings = copy.deepcopy(DEFAULT_SETTINGS)
                self.save()
                logger.info("Created default settings at %s", self.settings_path)
        except (json.JSONDecodeError, OSError) as e:
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
        if category not in self._settings:
            self._settings[category] = {}
        self._settings[category][key] = value
        self.save()

    def update_category(self, category: str, updates: Dict[str, Any]) -> None:
        """Update multiple settings in a category at once.

        Args:
            category: Settings category.
            updates: Dictionary of key-value pairs to update.
        """
        if category not in self._settings:
            self._settings[category] = {}
        self._settings[category].update(updates)
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


def projects_root() -> Path:
    """The main projects folder, and the only folder Docker may mount.

    Order: the root chosen in the dashboard, then SETUP_MANAGER_SAFE_ROOT,
    then the built-in default. An empty dashboard value means "use the next one".
    """
    configured = settings_manager.get("paths", "safe_root") or os.getenv("SETUP_MANAGER_SAFE_ROOT")
    default = str(Path.home() / "Projects")
    return Path(configured or default).expanduser().resolve()


def allowed_project_roots() -> list:
    """Folders a project path may live in.

    The dashboard choice is the main folder. When SETUP_MANAGER_SAFE_ROOT
    points somewhere else, that folder stays allowed too, so tools kept
    in a second folder can still be opened.
    """
    roots = [projects_root()]
    extra = os.getenv("SETUP_MANAGER_SAFE_ROOT")
    if not extra:
        return roots
    try:
        path = Path(extra).expanduser().resolve()
    except OSError:
        return roots
    if path.is_dir() and path not in roots:
        roots.append(path)
    return roots
