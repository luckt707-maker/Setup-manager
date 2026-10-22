from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import tkinter as tk
from tkinter import ttk

from .detector import ProjectDetector, QoLExtensionsRegistry
from .server_state import find_running_instance, read_state
from .setup import Setup
from .workspace import ProjectBootstrapper


class InteractiveCLI:
    """Interactive command-line interface"""

    def __init__(self):
        self.setup: Optional[Setup] = None
        self.detected_types = []

    def _launch_candidates(self, project_root: Path, detected: List[str]) -> List[Dict]:
        """Build conservative launch candidates for a detected project"""
        candidates = []

        if 'python' in detected:
            python_files = sorted(
                path for path in project_root.glob('*.py')
                if path.name not in {'__init__.py', 'setup.py'}
            )
            for path in python_files[:10]:
                candidates.append({
                    'label': f'Python: {path.name}',
                    'command': ['python', path.name],
                })

        package_file = project_root / 'package.json'
        if 'nodejs' in detected and package_file.exists():
            try:
                package = json.loads(package_file.read_text(encoding='utf-8'))
                scripts = package.get('scripts', {})
                for script_name in ('start', 'dev', 'serve'):
                    if script_name in scripts:
                        candidates.append({
                            'label': f'Node.js script: {script_name}',
                            'command': ['npm', 'run', script_name],
                        })
                        break

                main_file = package.get('main')
                if isinstance(main_file, str) and (project_root / main_file).is_file():
                    candidates.append({
                        'label': f'Node.js entry point: {main_file}',
                        'command': ['node', main_file],
                    })
            except (OSError, json.JSONDecodeError):
                print("⚠️ Could not read package.json; Node.js launch detection was skipped.")

        if 'dotnet' in detected:
            candidates.append({
                'label': '.NET project',
                'command': ['dotnet', 'run'],
            })

        if 'make' in detected:
            make_command = 'make' if shutil.which('make') else 'mingw32-make'
            candidates.append({
                'label': 'Make target: default',
                'command': [make_command],
            })

        if 'docker' in detected and any(
            (project_root / filename).exists()
            for filename in ('docker-compose.yml', 'docker-compose.yaml')
        ):
            candidates.append({
                'label': 'Docker Compose',
                'command': ['docker', 'compose', 'up'],
            })

        return candidates

    def analyze_folder(self, folder: str) -> None:
        """Analyze a folder and intelligently setup/launch a detected project"""
        folder_path = Path(folder.strip().strip('"')).expanduser()
        if not folder_path.is_dir():
            print(f"❌ Folder not found: {folder_path}")
            return

        folder_path = folder_path.resolve()
        self.setup = Setup(str(folder_path))
        self.detected_types = ProjectDetector(str(folder_path)).detect()

        print("\n🔎 FOLDER ANALYSIS")
        print("=" * 60)
        print(f"Folder: {folder_path}")
        print(f"Detected: {', '.join(self.detected_types) if self.detected_types else 'No supported project markers'}")

        if not self.detected_types:
            print("\nℹ️ No supported project types detected. No actions to take.")
            return

        # Intelligent Execution Priority
        priority_order = ['git', 'docker', 'python', 'nodejs', 'dotnet', 'make']
        sorted_detected = [t for t in priority_order if t in self.detected_types]
        
        print("\n🤖 INTELLIGENT SETUP SUGGESTION")
        print("-" * 60)
        print(f"Best execution path: {' → '.join(sorted_detected)}")
        
        try:
            confirm = input("\nWould you like to automatically run the best-fit setup for these? (y/n): ").strip().lower()
        except EOFError:
            # No terminal to talk to (piped input, a script, CI): do nothing
            # rather than treating the empty answer as a refusal.
            print("\nNo input available, skipping the automatic setup.")
            return
        if confirm == 'y':
            for env_type in sorted_detected:
                self.setup_environment(env_type)
            
            # Run quality checks for the primary language detected
            quality_types = [t for t in sorted_detected if t in {'python', 'nodejs', 'dotnet'}]
            if quality_types:
                print(f"\n✨ Running quality checks for primary environment: {quality_types[0]}...")
                results = self.setup.run_quality_checks(quality_types[0])
                self.display_check_results(results)
        else:
            print("\nSkipping automatic setup.")

        candidates = self._launch_candidates(folder_path, self.detected_types)
        if not candidates:
            print("\nℹ️ No launchable application entry point was detected.")
            return

        print("\n▶️ POSSIBLE PROGRAMS")
        for index, candidate in enumerate(candidates, 1):
            command = subprocess.list2cmdline(candidate['command'])
            print(f"  {index}. {candidate['label']} ({command})")
        print("  0. Do not run anything")

        choice = input("\nSelect a program to run: ").strip()
        if choice == '0' or not choice.isdigit() or not 1 <= int(choice) <= len(candidates):
            print("No program was started.")
            return

        candidate = candidates[int(choice) - 1]
        command = candidate['command']
        print(f"\n⚠️ This will execute code from: {folder_path}")
        print(f"Command: {subprocess.list2cmdline(command)}")
        confirm = input("Run this program? (y/n): ").strip().lower()
        if confirm == 'y':
            self.setup.executor.run(command, cwd=str(folder_path), show_output=True)
        else:
            print("Program launch cancelled.")

    def analyze_folder_prompt(self):
        """Read a dropped or pasted folder path from the terminal"""
        folder = input("Drop a project folder here and press Enter: ").strip()
        if folder:
            self.analyze_folder(folder)
        else:
            print("No folder was provided.")

    def manage_makefile(self):
        """Create or fetch a Makefile for the current folder"""
        project_root = Path.cwd().resolve()
        makefile_path = project_root / 'Makefile'

        print("\n🛠️ MAKEFILE MANAGER")
        print(f"Folder: {project_root}")
        print("1. Generate a safe Makefile template")
        print("2. Fetch a Makefile from an HTTPS URL")
        print("0. Back")
        choice = input("Enter your choice (0-2): ").strip()

        if choice == '1':
            template = """.PHONY: help

help:
\t@echo "Add project commands to this Makefile."
\t@echo "Available target: help"
"""
            if makefile_path.exists():
                confirm = input("Makefile exists. Overwrite it? (y/n): ").strip().lower()
                if confirm != 'y':
                    print("Makefile generation cancelled.")
                    return
            makefile_path.write_text(template, encoding='utf-8', newline='\n')
            print(f"✅ Safe Makefile template written to {makefile_path}")

        elif choice == '2':
            url = input("Enter an HTTPS Makefile URL: ").strip()
            parsed = urlparse(url)
            if parsed.scheme.lower() != 'https' or not parsed.netloc:
                print("❌ Only valid HTTPS URLs are allowed.")
                return

            print(f"URL: {url}")
            confirm = input("Fetch and save this Makefile? (y/n): ").strip().lower()
            if confirm != 'y':
                print("Makefile fetch cancelled.")
                return
            if makefile_path.exists():
                confirm = input("Makefile exists. Overwrite it? (y/n): ").strip().lower()
                if confirm != 'y':
                    print("Makefile fetch cancelled.")
                    return

            try:
                request = urllib.request.Request(
                    url,
                    headers={'User-Agent': 'SetupManager/1.0'},
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    content = response.read(1_048_577)
                if len(content) > 1_048_576:
                    print("❌ The Makefile is larger than the 1 MB safety limit.")
                    return
                makefile_path.write_text(content.decode('utf-8'), encoding='utf-8', newline='\n')
                print(f"✅ Makefile fetched to {makefile_path}")
                print("⚠️ Review the file before running make; Makefiles can execute commands.")
            except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as error:
                print(f"❌ Could not fetch the Makefile: {error}")

        elif choice != '0':
            print("❌ Invalid choice")

    def clear_screen(self):
        """Clear terminal screen"""
        os.system('cls')

    def display_header(self):
        """Display application header"""
        print("=" * 60)
        print("🚀 SETUP MANAGER - Development Environment Bootstrapper")
        print("=" * 60)

    def display_menu(self) -> str:
        """Display main menu and get user choice"""
        print("\n📋 MAIN MENU")
        print("-" * 40)
        print("1. Auto-detect and setup project")
        print("2. Manual project selection")
        print("3. Install QoL Extensions")
        print("4. Run Quality Checks")
        print("5. Folder navigation")
        print("6. Update all packages")
        print("7. Run custom command")
        print("8. About")
        print("9. Analyze dropped folder and optionally run it")
        print("10. Manage Makefile")
        print("11. 🌐 Launch Web Interface")
        print("12. 🌐 Network & host information")
        print("13. ⚡ Full stack auto bootstrap")
        print("0. Exit")
        print("-" * 40)

        return input("Enter your choice (0-13): ").strip()

    def display_actions_menu(self, detected: List[str]) -> str:
        """Display actions for detected project types"""
        print("\n📦 DETECTED PROJECT TYPES")
        print("-" * 40)
        print(f"Detected: {', '.join(detected) if detected else 'Unknown'}")
        print("-" * 40)
        print("1. Setup all detected environments")
        print("2. Select specific environment to setup")
        print("3. Install QoL Extensions")
        print("4. Run Quality Checks")
        print("5. Update packages")
        print("6. Run Make (if available)")
        print("7. Update Git")
        print("0. Back to main menu")
        print("-" * 40)

        return input("Enter your choice (0-7): ").strip()

    def display_environment_menu(self) -> str:
        """Display environment selection menu"""
        print("\n🔧 SELECT ENVIRONMENT")
        print("-" * 40)
        print("1. Python (venv + pip)")
        print("2. Node.js (npm/yarn/pnpm)")
        print("3. .NET (dotnet restore)")
        print("4. Make")
        print("5. Git")
        print("0. Back")
        print("-" * 40)

        return input("Enter your choice (0-5): ").strip()

    def display_extensions_menu(self) -> str:
        """Display QoL extensions selection menu"""
        print("\n📦 QoL EXTENSIONS INSTALLER")
        print("-" * 40)
        print("1. Install for Python")
        print("2. Install for Node.js")
        print("3. Install for .NET")
        print("4. Install for all detected")
        print("0. Back")
        print("-" * 40)

        return input("Enter your choice (0-4): ").strip()

    def display_checks_menu(self) -> str:
        """Display quality checks menu"""
        print("\n🔍 QUALITY CHECKS")
        print("-" * 40)
        print("1. Check Python project")
        print("2. Check Node.js project")
        print("3. Check .NET project")
        print("4. Check all detected projects")
        print("0. Back")
        print("-" * 40)

        return input("Enter your choice (0-4): ").strip()

    def show_extensions_for_type(self, project_type: str):
        """Show available extensions for a project type"""
        extensions = QoLExtensionsRegistry.get_extensions(project_type)

        if not extensions:
            print(f"\n❌ No extensions available for {project_type}")
            return

        print(f"\n📦 AVAILABLE EXTENSIONS FOR {project_type.upper()}")
        print("-" * 50)

        # Group by category
        by_category = {}
        for ext in extensions:
            # Try to infer category from command
            if 'lint' in ext['cmd'].lower() or 'flake' in ext['cmd'].lower() or 'pylint' in ext['cmd'].lower():
                cat = 'Linters'
            elif 'black' in ext['cmd'].lower() or 'prettier' in ext['cmd'].lower():
                cat = 'Formatters'
            elif 'test' in ext['cmd'].lower() or 'pytest' in ext['cmd'].lower():
                cat = 'Testing'
            elif 'type' in ext['cmd'].lower() or 'mypy' in ext['cmd'].lower():
                cat = 'Type Checking'
            elif 'security' in ext['cmd'].lower() or 'bandit' in ext['cmd'].lower() or 'audit' in ext['cmd'].lower():
                cat = 'Security'
            elif 'cProfile' in ext['cmd'] or 'prof' in ext['cmd'].lower():
                cat = 'Profiling'
            else:
                cat = 'Debuggers'

            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(ext)

        for category, exts in by_category.items():
            print(f"\n{category}:")
            for ext in exts:
                print(f"  • {ext['name']:<15} - {ext['desc']}")

        print("\n" + "-" * 50)

    def folder_navigation(self):
        """Handle folder navigation"""
        self.clear_screen()
        self.display_header()

        print("\n📁 FOLDER NAVIGATION")
        print("-" * 40)

        while True:
            current = os.getcwd()
            print(f"\nCurrent directory: {current}\n")

            try:
                items = os.listdir(current)
                dirs = [d for d in items if os.path.isdir(os.path.join(current, d))]
                dirs.sort()

                print("Directories:")
                for i, d in enumerate(dirs, 1):
                    print(f"  {i}. {d}/")

                print("\nOptions:")
                print("  U. Go up one folder")
                print("  P. Print current path")
                print("  0. Back to main menu")

                choice = input("\nEnter choice: ").strip().upper()

                if choice == '0':
                    break
                elif choice == 'U':
                    os.chdir('..')
                    self.clear_screen()
                    self.display_header()
                elif choice == 'P':
                    print(f"\nCurrent path:\n{os.getcwd()}")
                elif choice.isdigit() and 1 <= int(choice) <= len(dirs):
                    os.chdir(dirs[int(choice) - 1])
                    self.clear_screen()
                    self.display_header()
                else:
                    print("❌ Invalid choice")

            except EOFError:
                # The input stream ended (piped input, closed terminal).
                # Looping here would spin forever, so leave the menu.
                print()
                break
            except Exception as e:
                print(f"❌ Error: {e}")

    def run_custom_command(self):
        """Allow user to run custom commands"""
        print("\n💻 RUN CUSTOM COMMAND")
        print("-" * 40)
        cmd = input("Enter command to run: ").strip()

        if cmd:
            print("\n⚠️ WARNING: This runs with Windows shell access and can modify or delete files.")
            confirm = input("Run this command? (y/n): ").strip().lower()
            if confirm == 'y':
                setup = self.setup
                if setup is None:
                    setup = Setup()
                    self.setup = setup
                setup.executor.run(cmd, allow_shell=True)
            else:
                print("Command cancelled.")

        input("\nPress Enter to continue...")

    def setup_environment(self, env_type: str) -> bool:
        """Setup specific environment"""
        if self.setup is None:
            self.setup = Setup(interactive=True)

        handlers = {
            "python": self.setup.setup_python,
            "nodejs": self.setup.setup_nodejs,
            "dotnet": self.setup.setup_dotnet,
            "ruby": self.setup.setup_ruby,
            "git": self.setup.setup_git,
            "make": self.setup.setup_make,
        }

        if env_type in handlers:
            return handlers[env_type]()

        return False

    def auto_setup_all(self) -> bool:
        """Setup all detected environments"""
        print("\n⚙️ AUTO-SETUP ALL DETECTED ENVIRONMENTS")
        print("-" * 40)

        success = True

        setup_order = ['git', 'python', 'nodejs', 'dotnet', 'make']

        for env_type in setup_order:
            if env_type in self.detected_types:
                success &= self.setup_environment(env_type)

        if success:
            print("\n✅ All environments setup successfully!")

        return success

    def install_extensions(self):
        """Install QoL extensions"""
        while True:
            choice = self.display_extensions_menu()

            if choice == '0':
                break

            env_map = {'1': 'python', '2': 'nodejs', '3': 'dotnet'}

            if choice in env_map:
                project_type = env_map[choice]

                if self.setup is None:
                    self.setup = Setup(interactive=True)

                # Show available extensions
                self.show_extensions_for_type(project_type)

                # Ask to install
                confirm = input("\nInstall extensions for this environment? (y/n): ").strip().lower()
                if confirm == 'y':
                    self.setup.install_extensions(project_type)
                    input("\nPress Enter to continue...")

            elif choice == '4':
                # Install for all detected
                if self.setup is None:
                    self.setup = Setup(interactive=True)
                for env_type in self.detected_types:
                    if env_type in ['python', 'nodejs', 'dotnet']:
                        self.show_extensions_for_type(env_type)
                        self.setup.install_extensions(env_type)
                input("\nPress Enter to continue...")

            else:
                print("❌ Invalid choice")

    def run_quality_checks(self):
        """Run quality checks"""
        while True:
            choice = self.display_checks_menu()

            if choice == '0':
                break

            env_map = {'1': 'python', '2': 'nodejs', '3': 'dotnet'}

            if choice in env_map:
                project_type = env_map[choice]
                if self.setup is None:
                    self.setup = Setup(interactive=True)
                results = self.setup.run_quality_checks(project_type)
                self.display_check_results(results)
                input("\nPress Enter to continue...")

            elif choice == '4':
                # Check all detected
                if self.setup is None:
                    self.setup = Setup(interactive=True)
                for env_type in self.detected_types:
                    if env_type in ['python', 'nodejs', 'dotnet']:
                        results = self.setup.run_quality_checks(env_type)
                        self.display_check_results(results)
                input("\nPress Enter to continue...")

            else:
                print("❌ Invalid choice")

    def display_check_results(self, results: Dict[str, str]):
        """Display quality check results"""
        print("\n" + "=" * 50)
        print("📊 QUALITY CHECK RESULTS")
        print("=" * 50)

        passed = sum(1 for v in results.values() if v == 'pass')
        failed = sum(1 for v in results.values() if v == 'fail')
        skipped = sum(1 for v in results.values() if v == 'skip')

        for check, result in results.items():
            status = {
                'pass': "✅ PASS",
                'fail': "❌ FAIL",
                'skip': "⚪ SKIP",
            }.get(result, "❔ UNKNOWN")
            print(f"  {check:<20} {status}")

        print("-" * 50)
        print(f"Passed: {passed} | Failed: {failed} | Skipped: {skipped}")
        print("=" * 50)

    def select_and_setup(self):
        """Let user select which environment to setup"""
        while True:
            choice = self.display_environment_menu()

            env_map = {
                '1': 'python',
                '2': 'nodejs',
                '3': 'dotnet',
                '4': 'make',
                '5': 'git',
            }

            if choice == '0':
                break
            elif choice in env_map:
                self.setup_environment(env_map[choice])
                input("\nPress Enter to continue...")
            else:
                print("❌ Invalid choice")

    def display_about(self):
        """Display about information"""
        self.clear_screen()
        self.display_header()
        print("""
📖 ABOUT SETUP MANAGER

This tool helps you:
  • Auto-detect project types
  • Setup development environments
  • Install QoL Extensions for code quality
  • Run automated quality checks
  • Navigate folders and run commands
  • Manage packages and dependencies

Supported Environments:
  🐍 Python (pip + venv)
  🟢 Node.js (npm/yarn/pnpm)
  🔷 .NET (dotnet)
  🔨 Make/CMake
  🐙 Git
  🐳 Docker

QoL Extensions Include:
  ✓ Linters (pylint, flake8, eslint, StyleCop)
  ✓ Formatters (black, prettier)
  ✓ Type Checkers (mypy, TypeScript, Roslyn)
  ✓ Testing Frameworks (pytest, Jest, xUnit)
  ✓ Debuggers (pdb, ipdb, Node inspector, VS debugger)
  ✓ Profilers (cProfile, Node profiler)
  ✓ Security Scanners (bandit, npm audit)
        """)
        input("Press Enter to continue...")

    @staticmethod
    def _port_is_open(port: int) -> bool:
        """True when something is already listening on the given local port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    def launch_web_interface(self):
        """Start the web dashboard in a background process and open a browser.

        The dashboard may not end up on the preferred port - it moves up to
        the next free one - so the port it actually took is read back from
        the runtime state file rather than assumed.
        """
        import webbrowser

        app_path = Path(__file__).resolve().parent.parent / "app.py"
        if not app_path.exists():
            print(f"❌ Web interface not found: {app_path}")
            return False

        running = find_running_instance()
        if running:
            print(f"🌐 The dashboard is already running at {running['url']}")
            webbrowser.open(running["url"])
            return True

        print("🌐 Starting the web dashboard...")
        try:
            subprocess.Popen(
                [sys.executable, str(app_path)],
                cwd=str(app_path.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            print(f"❌ Could not start the web dashboard: {exc}")
            return False

        # The server picks a port and records it once it is listening.
        url = None
        for _ in range(40):
            time.sleep(0.25)
            state = read_state()
            if state and self._port_is_open(state["port"]):
                url = state.get("url") or f"http://127.0.0.1:{state['port']}"
                break

        if url is None:
            print(f"⚠️ The dashboard did not come up. "
                  f"Try running it manually: {sys.executable} {app_path}")
            return False

        webbrowser.open(url)
        print("✅ Browser opened. Close the dashboard's console window to stop it.")
        return True

    def show_network_info_window(self):
        """Opens a GUI window displaying Hostname and Local IP Address"""
        hostname = socket.gethostname()
        fqdn = socket.getfqdn()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "127.0.0.1"

        root = tk.Tk()
        root.title("Network & Host Information")
        root.geometry("400x180")
        root.resizable(False, False)
        root.configure(bg="#f0f0f0")

        style = ttk.Style()
        style.configure("TLabel", background="#f0f0f0", font=("Segoe UI", 11))
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))

        frame = ttk.Frame(root, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="🌐 Connection Info", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 10))
        ttk.Label(frame, text=f"Hostname:   {hostname}").pack(anchor=tk.W, pady=2)
        ttk.Label(frame, text=f"FQDN:       {fqdn}").pack(anchor=tk.W, pady=2)
        ttk.Label(frame, text=f"Local IP:   {local_ip}").pack(anchor=tk.W, pady=2)

        ttk.Button(frame, text="Close", command=root.destroy).pack(pady=(15, 0))
        root.mainloop()

    def run(self):
        """Run the interactive CLI"""
        self.clear_screen()

        while True:
            self.clear_screen()
            self.display_header()

            choice = self.display_menu()

            if choice == '0':
                print("\n👋 Goodbye!")
                break

            elif choice == '1':
                # Auto-detect and setup
                detector = ProjectDetector()
                self.detected_types = detector.detect()
                self.setup = Setup(interactive=True)

                self.clear_screen()
                self.display_header()

                actions_menu = self.display_actions_menu(self.detected_types)

                if actions_menu == '1':
                    self.auto_setup_all()
                elif actions_menu == '2':
                    self.select_and_setup()
                elif actions_menu == '3':
                    self.show_extensions_for_type(self.detected_types[0] if self.detected_types else 'python')
                    self.install_extensions()
                elif actions_menu == '4':
                    self.run_quality_checks()
                elif actions_menu == '5':
                    self.detected_types = detector.detect()
                    self.setup.update_packages()
                elif actions_menu == '6':
                    self.setup.setup_make()
                elif actions_menu == '7':
                    self.setup.setup_git()

                input("\nPress Enter to continue...")

            elif choice == '2':
                # Manual selection
                self.setup = Setup(interactive=True)
                self.select_and_setup()

            elif choice == '3':
                # Install QoL Extensions
                detector = ProjectDetector()
                self.detected_types = detector.detect()
                self.setup = Setup(interactive=True)
                self.install_extensions()

            elif choice == '4':
                # Run Quality Checks
                detector = ProjectDetector()
                self.detected_types = detector.detect()
                self.setup = Setup(interactive=True)
                self.run_quality_checks()

            elif choice == '5':
                # Folder navigation
                self.folder_navigation()

            elif choice == '6':
                # Update packages
                self.setup = Setup(interactive=True)
                self.setup.update_packages()
                input("\nPress Enter to continue...")

            elif choice == '7':
                # Custom command
                if not self.setup:
                    self.setup = Setup(interactive=True)
                self.run_custom_command()

            elif choice == '8':
                # About
                self.display_about()

            elif choice == '9':
                # Analyze a dropped or pasted folder
                self.analyze_folder_prompt()
                input("\nPress Enter to continue...")

            elif choice == '10':
                # Create or fetch a Makefile
                self.manage_makefile()
                input("\nPress Enter to continue...")

            elif choice == '11':
                # Start the web dashboard and open it in a browser
                self.launch_web_interface()
                input("\nPress Enter to continue...")

            elif choice == '12':
                # Hostname / local IP in a small GUI window
                self.show_network_info_window()

            elif choice == '13':
                # Detect a backend/frontend workspace and set both up
                folder = input("\nProject folder: ").strip()
                if not folder:
                    print("❌ No folder given.")
                else:
                    bootstrapper = ProjectBootstrapper(folder)
                    bootstrapper.bootstrap()
                input("\nPress Enter to continue...")

            else:
                print("❌ Invalid choice")
                input("Press Enter to continue...")


def main():
    """Main entry point"""
    try:
        cli = InteractiveCLI()
        if len(sys.argv) >= 3 and sys.argv[1] == '--folder':
            cli.analyze_folder(sys.argv[2])
            return
        cli.run()
    except KeyboardInterrupt:
        print("\n\n👋 Interrupted by user. Goodbye!")
        sys.exit(0)
    except EOFError:
        print("\n👋 No input available (stdin was closed). Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
