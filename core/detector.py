from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .language_templates import get_all_markers, get_all_qol, get_all_install_prefixes


class ExtensionType(Enum):
    """Types of QoL extensions"""
    LINTER = "linter"
    FORMATTER = "formatter"
    TESTER = "testing"
    DEBUGGER = "debugger"
    PROFILER = "profiler"
    SECURITY = "security"
    TYPE_CHECKER = "type_checker"
    ANALYZER = "analyzer"


class QoLExtensionsRegistry:
    """Registry of QoL extensions for different project types.

    Extensions are loaded from language_templates.py so that adding a new
    language requires editing only the template definition.
    """

    @staticmethod
    def _build_extensions() -> Dict[str, Dict[str, List[Dict[str, str]]]]:
        """Build the extensions dict from language templates."""
        return get_all_qol()

    EXTENSIONS = _build_extensions()

    @staticmethod
    def get_extensions(project_type: str, ext_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get extensions for a project type"""
        if project_type not in QoLExtensionsRegistry.EXTENSIONS:
            return []

        exts = QoLExtensionsRegistry.EXTENSIONS[project_type]

        if ext_type:
            return exts.get(ext_type, [])

        # Return all extensions for this project type
        all_exts = []
        for category in exts.values():
            all_exts.extend(category)
        return all_exts


class ProjectDetector:
    """Detect project types based on files in directory.

    Detectors are loaded from language_templates.py so that adding a new
    language requires editing only the template definition.
    """

    @staticmethod
    def _build_detectors() -> Dict[str, List[str]]:
        """Build the detectors dict from language templates."""
        return get_all_markers()

    DETECTORS = _build_detectors()

    def __init__(self, path: str = '.'):
        self.path = Path(path)

    def detect(self) -> List[str]:
        """Detect project types in directory"""
        detected = []

        for project_type, markers in self.DETECTORS.items():
            for marker in markers:
                # Handle glob patterns
                if '*' in marker:
                    if list(self.path.glob(marker)):
                        detected.append(project_type)
                        break
                else:
                    if (self.path / marker).exists():
                        detected.append(project_type)
                        break

        return list(dict.fromkeys(detected))  # Remove duplicates while preserving detector order
