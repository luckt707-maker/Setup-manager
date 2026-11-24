"""Create a Desktop shortcut that starts the Setup Manager dashboard.

  venv\\Scripts\\python.exe create_shortcut.py

The shortcut launches the dashboard without a console window, opens the
user interface, and points the app at the folder it is allowed to work in.
Running it twice is harmless: the existing shortcut is replaced.
"""
import subprocess
import sys
from pathlib import Path

from core.settings import projects_root, projects_root_notes

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "app.py"
SHORTCUT_NAME = "Setup Manager.lnk"


def _pick_python(root: Path, python_exe: str = None) -> Path:
    """Prefer the venv's windowless python, then the caller's, then ours."""
    if python_exe:
        return Path(python_exe)
    venv_pythonw = root / "venv" / "Scripts" / "pythonw.exe"
    if venv_pythonw.exists():
        return venv_pythonw
    return Path(sys.executable)


def shortcut_spec(root: Path = None, python_exe: str = None) -> dict:
    """Describe the shortcut: what it runs, where, and with which arguments.

    Kept separate from the shortcut writing so it can be checked without
    touching the user's Desktop. The options are passed as arguments
    because a shortcut cannot be relied on to carry an environment.
    """
    root = Path(root or ROOT)
    target = _pick_python(root, python_exe)
    # Same folder the dashboard will use. Never the folder the zip was unpacked into.
    safe_root = str(projects_root())
    return {
        "target": str(target),
        "arguments": f'"{root / "app.py"}" --auto-open --safe-root "{safe_root}"',
        "working_directory": str(root),
        "icon": f"{target}, 0",
        "description": "Setup Manager - project setup, diagnostics and AI assistant",
    }


def _ps_quote(value: str) -> str:
    """Quote a value for PowerShell. Single quotes survive any path."""
    return "'" + str(value).replace("'", "''") + "'"


def _powershell_script(spec: dict, shortcut_path: Path) -> str:
    """Build the PowerShell that writes the .lnk."""
    return f"""
    $ErrorActionPreference = 'Stop'
    $WshShell = New-Object -comObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut({_ps_quote(shortcut_path)})
    $Shortcut.TargetPath = {_ps_quote(spec["target"])}
    $Shortcut.Arguments = {_ps_quote(spec["arguments"])}
    $Shortcut.WorkingDirectory = {_ps_quote(spec["working_directory"])}
    $Shortcut.IconLocation = {_ps_quote(spec["icon"])}
    $Shortcut.Description = {_ps_quote(spec["description"])}
    $Shortcut.WindowStyle = 7
    $Shortcut.Save()
    """


def _write_fallback_bat(spec: dict, desktop: Path) -> Path:
    """Write a .bat that does the same job, for when COM is unavailable."""
    bat_path = desktop / "Setup Manager.bat"
    bat_path.write_text(
        "@echo off\n"
        "chcp 65001 >nul\n"
        "title Setup Manager\n"
        f'cd /d "{spec["working_directory"]}"\n'
        f'start "" "{spec["target"]}" {spec["arguments"]}\n',
        encoding="utf-8",
    )
    return bat_path


def create_desktop_shortcut(root: Path = None, python_exe: str = None) -> Path:
    """Write the Desktop shortcut. Returns the path it created."""
    root = Path(root or ROOT)
    spec = shortcut_spec(root, python_exe)
    if not TARGET.exists():
        print(f"❌ Could not find {TARGET}.")
        return None

    desktop = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    shortcut_path = desktop / SHORTCUT_NAME

    print(f"🔧 Creating a shortcut for {TARGET.name}...")
    for line in projects_root_notes():
        print(f"   {line}")
    print(f"   Runs:      {spec['target']} {spec['arguments']}")
    print(f"   Folder:    {spec['working_directory']}")

    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                _powershell_script(spec, shortcut_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as exc:
        print(f"⚠️ PowerShell shortcut creation failed: {exc}")
        print("🔄 Creating a fallback .bat file instead...")
        bat_path = _write_fallback_bat(spec, desktop)
        print(f"✅ Created {bat_path}")
        return bat_path

    print(f"✅ Created {shortcut_path}")
    print("   Double-click it to open the dashboard.")
    return shortcut_path


if __name__ == "__main__":
    create_desktop_shortcut()
    if "--pause" in sys.argv and sys.stdin.isatty():
        input("\nPress Enter to exit...")
