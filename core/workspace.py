"""Workspace layout detection and full-stack project bootstrapping.

A "workspace" here is a folder that holds more than one project: typically a
backend service and a frontend app side by side. The bootstrapper detects that
layout and runs the matching setup for each half.
"""
from pathlib import Path
from typing import Any, Dict, Optional

from .setup import Setup


class WorkspaceDetector:
    """Detects frontend and backend directories in a workspace."""

    def __init__(self, root_path: Path):
        self.root = Path(root_path)

    def detect(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "backend": None,
            "frontend": None,
            "backend_type": None,
        }
        if not self.root.is_dir():
            return result
        for item in sorted(self.root.iterdir()):
            if not item.is_dir() or item.name.startswith((".", "$")):
                continue
            if (item / "requirements.txt").exists() or (item / "pyproject.toml").exists():
                if result["backend"] is None:
                    result["backend"] = item
                    result["backend_type"] = "python"
            elif (item / "package.json").exists():
                if result["frontend"] is None:
                    result["frontend"] = item
        return result


class ProjectBootstrapper:
    """Sets up every project half of a workspace."""

    def __init__(self, folder: str):
        self.folder = Path(folder)

    def detect(self) -> Dict[str, Any]:
        return WorkspaceDetector(self.folder).detect()

    def bootstrap(self, callback: Optional[Any] = None) -> bool:
        """Set up the detected backend and frontend. Returns True if all succeeded."""
        workspace = self.detect()
        backend = workspace.get("backend")
        frontend = workspace.get("frontend")

        if not backend and not frontend:
            print("❌ No backend or frontend project found in this folder.")
            return False

        success = True
        if backend:
            print(f"\n📦 Setting up backend: {backend}")
            backend_setup = Setup(str(backend), interactive=True)
            if workspace.get("backend_type") == "python":
                success &= bool(backend_setup.setup_python(callback))
            else:
                success &= bool(backend_setup.setup_nodejs(callback))

        if frontend:
            print(f"\n📦 Setting up frontend: {frontend}")
            frontend_setup = Setup(str(frontend), interactive=True)
            success &= bool(frontend_setup.setup_nodejs(callback))

        if success:
            print("\n✅ Workspace bootstrap complete.")
        else:
            print("\n⚠️ Workspace bootstrap finished with errors.")
        return success
