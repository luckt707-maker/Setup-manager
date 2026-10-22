"""Create a Desktop shortcut that starts the Setup Manager dashboard.

  venv\\Scripts\\python.exe create_shortcut.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "app.py"


def create_desktop_shortcut():
    if not TARGET.exists():
        print(f"❌ Could not find {TARGET}.")
        return

    python_exe = sys.executable
    desktop = Path.home() / "Desktop"
    shortcut_path = desktop / "Setup Manager.lnk"

    # PowerShell command to create a native Windows .lnk shortcut.
    # The execution policy is left alone: it governs script files, not the
    # inline command passed to -Command.
    ps_command = f"""
    $WshShell = New-Object -comObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
    $Shortcut.TargetPath = "{python_exe}"
    $Shortcut.Arguments = "{TARGET}"
    $Shortcut.WorkingDirectory = "{ROOT}"
    $Shortcut.IconLocation = "{python_exe}, 0"
    $Shortcut.Description = "Launch Setup Manager Web API"
    $Shortcut.Save()
    """

    print(f"🔧 Attempting to create shortcut for: {TARGET.name}")

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_command],
            check=True,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        print("✅ Success! Shortcut created on your Desktop: 'Setup Manager.lnk'")
        print("   You can now double-click it to launch the app.")
    except Exception as e:
        print(f"⚠️ PowerShell shortcut creation failed: {e}")
        print("🔄 Creating a fallback .bat file instead...")

        bat_path = desktop / "Setup Manager.bat"
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(
                f'@echo off\nchcp 65001 >nul\ntitle Setup Manager\n'
                f'"{python_exe}" "{TARGET}"\n'
            )
        print(f"✅ Success! Fallback batch file created on your Desktop: 'Setup Manager.bat'")


if __name__ == "__main__":
    create_desktop_shortcut()
    input("\nPress Enter to exit...")
