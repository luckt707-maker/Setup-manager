from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import subprocess
import sys

from .cmdline import split_command
from .dependencies import (
    declared_requirements,
    find_venv,
    install_arguments,
    installed_distributions,
    normalize_name,
    site_packages_dirs,
)
from .detector import ProjectDetector, QoLExtensionsRegistry
from .executor import CommandExecutor
from .language_templates import get_all_install_prefixes, get_template
from .tooling import gradle_command


def source_retry_command(command: List[str], project: Path) -> Optional[List[str]]:
    """The same pip install without ``--only-binary``.

    The first attempt stays wheel-only so a local path cannot run the
    project's setup code. Some packages, stem among them, publish no
    Windows wheel. Those can be built from the package index. A path,
    an editable install, or a requirements file that points at one is
    not retried.
    """
    if "--only-binary=:all:" not in command or "install" not in command:
        return None
    if not _index_packages_only(command, Path(project)):
        return None
    return [part for part in command if part != "--only-binary=:all:"]


def _index_packages_only(command: List[str], project: Path) -> bool:
    args = [str(part) for part in command[command.index("install") + 1:]
            if part != "--only-binary=:all:"]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-r", "--requirement", "-c", "--constraint"}:
            index += 1
            if index >= len(args):
                return False
            if arg in {"-c", "--constraint"}:
                index += 1
                continue
            path = Path(args[index])
            if not path.is_absolute():
                path = project / path
            if not _requirement_file_is_index_only(path, set()):
                return False
            index += 1
            continue
        if arg in {"-e", "--editable"} or arg.startswith(("-e", "--editable=")):
            return False
        if arg.startswith("-"):
            index += 1
            continue
        if _is_not_an_index_package(arg):
            return False
        index += 1
    return True


def _is_not_an_index_package(arg: str) -> bool:
    lowered = arg.lower()
    if arg in {".", ".."} or arg.startswith((".", "/", "\\")):
        return True
    if "\\" in arg or (len(arg) > 1 and arg[1] == ":"):
        return True
    if "://" in arg or lowered.startswith(("git+", "hg+", "svn+", "bzr+")):
        return True
    return False


def _requirement_file_is_index_only(path: Path, seen: set) -> bool:
    key = str(path.resolve()) if path.exists() else str(path)
    if key in seen:
        return True
    seen.add(key)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        parts = stripped.split()
        head = parts[0]
        if head in {"-c", "--constraint"}:
            continue
        if head in {"-r", "--requirement"}:
            if len(parts) < 2:
                return False
            child = Path(parts[1])
            if not child.is_absolute():
                child = path.parent / child
            if not _requirement_file_is_index_only(child, seen):
                return False
            continue
        if head in {"-e", "--editable"} or head.startswith(("-e", "--editable=")):
            return False
        if _is_not_an_index_package(head):
            return False
    return True


class Setup:
    """Setup handlers for different project types"""

    QUALITY_CHECK_CATEGORIES = ('linters', 'type_checkers', 'testers', 'security')

    # Both pytest and `python -m unittest discover` exit with 5 when they
    # found no tests to run. For a project that uses a different test runner
    # that is the normal case, not a failure, so it is reported as a skip.
    NO_TESTS_EXIT_CODE = 5

    # Install prefixes are loaded from language_templates.py so that adding
    # a new language requires editing only the template definition.
    @staticmethod
    def _build_install_prefixes() -> Dict[str, tuple]:
        """Build install prefixes from language templates."""
        raw = get_all_install_prefixes()
        return {k: tuple(v) for k, v in raw.items()}

    INSTALL_PREFIXES = _build_install_prefixes()

    def __init__(self, project_path: str = '.', interactive: bool = False):
        self.project_path = project_path
        self.executor = CommandExecutor()
        self.installed_extensions = {}
        # interactive=True only for the terminal CLI; the web API must never
        # block on input().
        self.interactive = interactive

    @staticmethod
    def _emit(callback: Optional[Callable[[str], None]], message: str) -> None:
        if callback:
            callback(message)
        else:
            print(message)

    def _pip_install(
        self,
        python: Path,
        arguments: List[str],
        callback: Optional[Callable[[str], None]],
    ) -> bool:
        """Install from wheels, then from source when a package has no wheel."""
        command = [str(python), "-m", "pip", "install", "--only-binary=:all:", *arguments]
        if self.executor.run(command, cwd=self.project_path, callback=callback):
            return True
        retry = source_retry_command(command, Path(self.project_path))
        if retry is None:
            return False
        self._emit(
            callback,
            "  One package has no wheel. Retrying so pip can build that package from source.",
        )
        return self.executor.run(retry, cwd=self.project_path, callback=callback)

    def install_extensions(
        self,
        project_type: str,
        ext_types: Optional[List[str]] = None,
        callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Install QoL extensions for a project type"""
        msg = f"\n📦 Installing QoL Extensions for {project_type}..."
        if callback:
            callback(msg)
        else:
            print(msg)

        if ext_types:
            extensions = []
            for ext_type in ext_types:
                extensions.extend(
                    QoLExtensionsRegistry.get_extensions(project_type, ext_type)
                )
        else:
            extensions = QoLExtensionsRegistry.get_extensions(project_type)

        if not extensions:
            msg = f"ℹ️ No extensions available for {project_type}"
            if callback:
                callback(msg)
            else:
                print(msg)
            return True

        success = True

        for ext in extensions:
            msg = f"\n  📍 Installing {ext['name']} ({ext['desc']})..."
            if callback:
                callback(msg)
            else:
                print(msg)
            install_command = ext['cmd']

            if not self._is_install_command(project_type, install_command):
                msg = f"  ⏭️ Skipping {ext['name']}: it is not something to install"
                if callback:
                    callback(msg)
                else:
                    print(msg)
                continue

            if project_type == 'python' and install_command.startswith(('pip ', 'pip3 ')):
                python_executable = self._venv_python()
                if python_executable is None:
                    msg = (
                        "  ⏭️ Skipping Python packages: this project has no virtual "
                        "environment. Run 'Set up Python' first."
                    )
                    if callback:
                        callback(msg)
                    else:
                        print(msg)
                    continue
                pip_args = split_command(install_command)[1:]
                install_command = [str(python_executable), '-m', 'pip', *pip_args]

            if self.executor.run(install_command, cwd=self.project_path, show_output=False, callback=callback):
                msg = f"  ✅ {ext['name']} installed"
                if callback:
                    callback(msg)
                else:
                    print(msg)
                self.installed_extensions[ext['name']] = ext
            else:
                msg = f"  ⚠️ Failed to install {ext['name']}"
                if callback:
                    callback(msg)
                else:
                    print(msg)
                success = False

        return success

    def _is_install_command(self, project_type: str, command: str) -> bool:
        """Is this command an installation, or something the project just runs?"""
        prefixes = self.INSTALL_PREFIXES.get(project_type, ())
        return any(command.startswith(prefix) for prefix in prefixes)

    def _venv_python(self) -> Optional[Path]:
        """The project's own Python interpreter.

        Setup Manager never installs into its own environment: a project's
        packages belong to the project, so this returns None when the
        project has no environment of its own.
        """
        root = Path(self.project_path).expanduser().resolve()
        for folder in ('venv', '.venv'):
            for candidate in (
                root / folder / 'Scripts' / 'python.exe',
                root / folder / 'bin' / 'python',
                root / folder / 'bin' / 'python3',
            ):
                if candidate.exists():
                    return candidate
        return None

    def _quality_command(self, command: str) -> list:
        """Use the project virtual environment for Python quality tools"""
        tokens = split_command(command)
        venv_python = self._venv_python()

        if venv_python is None or not tokens:
            return tokens

        python_tools = {
            'pylint', 'flake8', 'black', 'mypy', 'pytest', 'bandit', 'ipdb'
        }
        if tokens[0] in python_tools:
            return [str(venv_python), '-m', tokens[0], *tokens[1:]]
        if tokens[0] == 'python':
            return [str(venv_python), *tokens[1:]]
        return tokens

    def _check_available(self, command: list) -> bool:
        """Whether the tool a quality check needs is actually installed.

        ``_quality_command`` rewrites Python tools to ``<venv python> -m
        <tool>``. A plain "is this executable on PATH" check would always
        say yes there, because the interpreter is present even when the
        tool is not - so a missing linter was reported as a failing check
        instead of a skipped one. For that form, ask the interpreter.
        """
        if (
            len(command) > 2
            and command[1] == '-m'
            and Path(command[0]).name.lower().startswith('python')
        ):
            return self._module_available(command[0], command[2])
        return self.executor.check_installed(command)

    @staticmethod
    def _module_available(python: str, module: str) -> bool:
        """Ask an interpreter whether a module can be imported."""
        probe = (
            "import importlib.util, sys\n"
            "try:\n"
            "    spec = importlib.util.find_spec(sys.argv[1])\n"
            "except Exception:\n"
            "    spec = None\n"
            "sys.exit(0 if spec else 1)\n"
        )
        try:
            result = subprocess.run(
                [python, '-c', probe, module],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            # Cannot tell - let the command run and speak for itself.
            return True
        return result.returncode == 0

    def _has_python_tests(self) -> bool:
        """Check whether the project contains common Python test targets"""
        project_root = Path(self.project_path)
        return (
            (project_root / 'tests').is_dir()
            or any(project_root.glob('test*.py'))
            or any(project_root.glob('*_test.py'))
        )

    def _uses_pytest(self) -> bool:
        """Whether the project is set up for pytest.

        A pytest project that is also run through unittest gives two
        verdicts about the same tests, and unittest is the misleading one:
        it imports every package in the tree, so a package that cannot be
        imported is reported as a failing test.
        """
        project_root = Path(self.project_path)
        if (project_root / 'pytest.ini').is_file() or (project_root / 'conftest.py').is_file():
            return True
        for name in ('tox.ini', 'setup.cfg'):
            path = project_root / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            if '[pytest]' in text or '[tool:pytest]' in text:
                return True
        pyproject = project_root / 'pyproject.toml'
        if pyproject.is_file():
            try:
                import tomllib

                with pyproject.open('rb') as handle:
                    data = tomllib.load(handle)
            except (OSError, ValueError, ImportError):
                return False
            return 'pytest' in ((data.get('tool') or {}))
        return False

    def _missing_dependencies(self) -> List[str]:
        """The packages the project declares that are not installed.

        This is the answer to "No module named tomli": the project asked for
        tomli and the environment does not have it.
        """
        project_root = Path(self.project_path).expanduser().resolve()
        declared = declared_requirements(project_root)
        if not declared.requirements:
            return []
        venv = find_venv(project_root)
        if venv is None:
            return [requirement.name for requirement in declared.requirements]
        installed = installed_distributions(site_packages_dirs(venv))
        return [
            requirement.name
            for requirement in declared.requirements
            if normalize_name(requirement.name) not in installed
        ]

    def python_install_requirements(self) -> List[str]:
        """What to give pip so the program can run.

        Runtime requirement files and the main dependency table are
        included. Development groups, test lists, and optional extras
        are left out.
        """
        project_root = Path(self.project_path).expanduser().resolve()
        return install_arguments(project_root)

    def missing_requirements(self) -> List[Any]:
        """The declared dependencies that are not installed, with their constraints."""
        project_root = Path(self.project_path).expanduser().resolve()
        declared = declared_requirements(project_root)
        if not declared.requirements:
            return []
        venv = find_venv(project_root)
        if venv is None:
            return list(declared.requirements)
        installed = installed_distributions(site_packages_dirs(venv))
        return [
            requirement
            for requirement in declared.requirements
            if normalize_name(requirement.name) not in installed
        ]

    def install_missing_packages(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Install the packages the project declares but does not have.

        Only packages the project actually declares are installed: a module
        the code imports but does not declare might be a package, or it
        might be a file that belongs in the project, and pip cannot tell
        the difference.
        """
        missing = self.missing_requirements()
        if not missing:
            self._emit(callback, "✅ Every declared dependency is already installed.")
            return True

        venv_python = self._venv_python()
        if venv_python is None:
            self._emit(
                callback,
                "⏭️ Skipping: this project has no virtual environment. Run 'Set up Python' first.",
            )
            return False

        names = ", ".join(requirement.name for requirement in missing)
        self._emit(callback, f"📦 Installing the missing dependencies: {names}")
        return self._pip_install(
            venv_python,
            [requirement.to_pip() for requirement in missing],
            callback,
        )

    def run_quality_checks(
        self,
        project_type: str,
        callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, str]:
        """Run all available quality checks"""
        msg = f"\n🔍 Running Quality Checks for {project_type}..."
        if callback:
            callback(msg)
        else:
            print(msg)

        extensions = []
        for category in self.QUALITY_CHECK_CATEGORIES:
            for ext in QoLExtensionsRegistry.get_extensions(project_type, category):
                extensions.append((category, ext))
        results = {}

        # Say this before the checks run, not after they fail: "No module
        # named tomli" is a missing dependency, and the user should not have
        # to read a traceback to work that out.
        missing = self._missing_dependencies()
        if missing:
            msg = (
                f"\n  ⚠️ The project's dependencies are not installed: {', '.join(missing)}. "
                "Run 'Set up Python' first, or the checks below will fail."
            )
            if callback:
                callback(msg)
            else:
                print(msg)

        for category, ext in extensions:
            name = ext['name']
            if project_type == 'python' and name in {'pytest', 'unittest'} and not self._has_python_tests():
                msg = f"\n  ⚪ Skipping {name}: no Python test files were found"
                if callback:
                    callback(msg)
                else:
                    print(msg)
                results[name] = 'skip'
                continue

            if project_type == 'python' and name == 'unittest' and self._uses_pytest():
                # unittest would give a second, wrong verdict about the
                # same tests, so it does not run at all.
                msg = "\n  ⚪ Skipping unittest: this project uses pytest"
                if callback:
                    callback(msg)
                else:
                    print(msg)
                continue

            command = self._quality_command(ext['run'])
            if not self._check_available(command):
                msg = f"\n  ⚪ Skipping {name}: tool is not installed"
                if callback:
                    callback(msg)
                else:
                    print(msg)
                msg2 = "     Use Install QoL Extensions first, then run checks again."
                if callback:
                    callback(msg2)
                else:
                    print(msg2)
                results[name] = 'skip'
                continue

            msg = f"\n  🔄 Running {name}..."
            if callback:
                callback(msg)
            else:
                print(msg)
            # A test runner that collected no tests exits with 5; that is a
            # skip, not a failure.
            is_tester = category == 'testers'
            success, exit_code = self.executor.run_with_code(
                command,
                cwd=self.project_path,
                show_output=True,
                callback=callback,
                skip_exit_codes={self.NO_TESTS_EXIT_CODE} if is_tester else None,
            )
            if success:
                results[name] = 'pass'
            elif is_tester and exit_code == self.NO_TESTS_EXIT_CODE:
                msg = f"\n  ⚪ Skipping {name}: no tests were found to run"
                if callback:
                    callback(msg)
                else:
                    print(msg)
                results[name] = 'skip'
            else:
                results[name] = 'fail'

        return results

    def setup_python(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup Python project with venv"""
        self._emit(callback, "\n📦 Setting up Python environment...")
        project_root = Path(self.project_path).expanduser().resolve()
        venv_dir = project_root / 'venv'
        is_junction = getattr(venv_dir, 'is_junction', lambda: False)
        if venv_dir.is_symlink() or is_junction():
            self._emit(callback, "❌ Refusing to use a symlink or junction for the virtual environment.")
            return False
        if venv_dir.exists() and not venv_dir.is_dir():
            self._emit(callback, "❌ The virtual-environment path exists and is not a directory.")
            return False

        if not self.executor.run(
            [sys.executable, '-m', 'venv', str(venv_dir)], cwd=str(project_root), callback=callback):
            return False

        venv_python = venv_dir / 'Scripts' / 'python.exe'
        if not self.executor.run(
            [str(venv_python), '-m', 'pip', 'install', '--upgrade', 'pip'],
            cwd=self.project_path, callback=callback):
            return False

        # Only what the program needs to run. Dev, test, and optional
        # extras are not downloaded. --only-binary keeps pip from building
        # a source distribution, which would run the project's setup.py.
        arguments = install_arguments(project_root)
        if arguments:
            self._emit(callback, "  📦 Installing what the program needs to run.")
            if not self._pip_install(venv_python, arguments, callback):
                return False

        return True

    def setup_nodejs(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup Node.js project"""
        self._emit(callback, "\n📦 Setting up Node.js environment...")
        if not self.executor.check_installed("node"):
            self._emit(callback, "❌ Node.js is not installed. Please install it first.")
            return False

        # Detect package manager
        project_root = Path(self.project_path)
        # --ignore-scripts: a package's install script runs whatever it
        # likes, and the packages come from the project's manifest.
        if (project_root / 'yarn.lock').exists():
            return self.executor.run("yarn install --ignore-scripts", cwd=self.project_path, callback=callback)
        elif (project_root / 'pnpm-lock.yaml').exists():
            return self.executor.run("pnpm install --ignore-scripts", cwd=self.project_path, callback=callback)
        else:
            return self.executor.run("npm install --ignore-scripts", cwd=self.project_path, callback=callback)

    def setup_dotnet(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup .NET project"""
        self._emit(callback, "\n📦 Setting up .NET environment...")
        if not self.executor.check_installed("dotnet"):
            self._emit(callback, "❌ .NET SDK is not installed. Please install it first.")
            return False

        return self.executor.run("dotnet restore", cwd=self.project_path, callback=callback)

    def setup_ruby(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup Ruby project with Bundler"""
        msg = "\n📦 Setting up Ruby environment..."
        if callback:
            callback(msg)
        else:
            print(msg)

        # Install bundler
        if not self.executor.run(["gem", "install", "bundler", "--no-document"], cwd=self.project_path, callback=callback):
            return False

        # Install dependencies
        if not self.executor.run(["bundle", "install"], cwd=self.project_path, callback=callback):
            return False

        return True

    def setup_java(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup Java project with Maven or Gradle"""
        self._emit(callback, "\n📦 Setting up Java environment...")
        if not self.executor.check_installed("java"):
            self._emit(callback, "❌ Java is not installed. Please install it first.")
            return False

        project_root = Path(self.project_path)
        if (project_root / "build.gradle.kts").exists() or (project_root / "build.gradle").exists():
            return self.executor.run(
                gradle_command(project_root, "build"), cwd=self.project_path, callback=callback
            )
        elif (project_root / "pom.xml").exists():
            return self.executor.run("mvn clean install", cwd=self.project_path, callback=callback)
        else:
            self._emit(callback, "⚠️ No Maven or Gradle build file found.")
            return False

    def setup_go(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup Go project"""
        self._emit(callback, "\n📦 Setting up Go environment...")
        if not self.executor.check_installed("go"):
            self._emit(callback, "❌ Go is not installed. Please install it first.")
            return False

        project_root = Path(self.project_path)
        if not (project_root / "go.mod").exists():
            self._emit(callback, "⚠️ No go.mod found. Run 'go mod init' first.")
            return False

        return self.executor.run("go mod tidy", cwd=self.project_path, callback=callback)

    def setup_rust(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup Rust project"""
        self._emit(callback, "\n📦 Setting up Rust environment...")
        if not self.executor.check_installed("cargo"):
            self._emit(callback, "❌ Rust/Cargo is not installed. Please install it first.")
            return False

        project_root = Path(self.project_path)
        if not (project_root / "Cargo.toml").exists():
            self._emit(callback, "⚠️ No Cargo.toml found.")
            return False

        return self.executor.run("cargo build", cwd=self.project_path, callback=callback)

    def setup_php(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup PHP project with Composer"""
        self._emit(callback, "\n📦 Setting up PHP environment...")
        if not self.executor.check_installed("php"):
            self._emit(callback, "❌ PHP is not installed. Please install it first.")
            return False

        if not self.executor.check_installed("composer"):
            self._emit(callback, "❌ Composer is not installed. Please install it first.")
            return False

        project_root = Path(self.project_path)
        if not (project_root / "composer.json").exists():
            self._emit(callback, "⚠️ No composer.json found.")
            return False

        return self.executor.run("composer install", cwd=self.project_path, callback=callback)

    def setup_typescript(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup TypeScript project"""
        self._emit(callback, "\n📦 Setting up TypeScript environment...")
        if not self.executor.check_installed("node"):
            self._emit(callback, "❌ Node.js is not installed. Please install it first.")
            return False

        return self.executor.run("npm install", cwd=self.project_path, callback=callback)

    def setup_kotlin(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup Kotlin project"""
        self._emit(callback, "\n📦 Setting up Kotlin environment...")
        if not self.executor.check_installed("java"):
            self._emit(callback, "❌ Java is not installed. Please install it first.")
            return False

        return self.executor.run(
            gradle_command(Path(self.project_path), "build"),
            cwd=self.project_path,
            callback=callback,
        )

    def setup_swift(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup Swift project"""
        self._emit(callback, "\n📦 Setting up Swift environment...")
        if not self.executor.check_installed("swift"):
            self._emit(callback, "❌ Swift is not installed. Please install it first.")
            return False

        return self.executor.run("swift build", cwd=self.project_path, callback=callback)

    def setup_scala(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Setup Scala project"""
        self._emit(callback, "\n📦 Setting up Scala environment...")
        if not self.executor.check_installed("java"):
            self._emit(callback, "❌ Java is not installed. Please install it first.")
            return False

        if not self.executor.check_installed("sbt"):
            self._emit(callback, "❌ sbt is not installed. Please install it first.")
            return False

        return self.executor.run("sbt compile", cwd=self.project_path, callback=callback)

    def _make_command(self):
        """Return the available Make executable name"""
        for command in ('make', 'mingw32-make'):
            if self.executor.check_installed(command):
                return command
        return None

    def _install_make(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Offer to install Make through an available Windows package manager"""
        installers = [
            ('winget', [
                'winget', 'install', '--id', 'GnuWin32.Make', '--exact',
                '--accept-source-agreements', '--accept-package-agreements',
            ]),
            ('Chocolatey', ['choco', 'install', 'make', '-y']),
            ('MSYS2 pacman', ['pacman', '-S', '--needed', '--noconfirm', 'make']),
        ]
        available = [item for item in installers if self.executor.check_installed(item[1])]
        if not available:
            self._emit(callback, "❌ No supported package manager was found (winget, Chocolatey, or MSYS2).")
            return False

        name, command = available[0]
        self._emit(callback, f"Make is not installed. Available installer: {name}")
        if not self.interactive:
            self._emit(callback, f"❌ Make is missing. Install it with {name} (run this tool as Administrator if needed), then retry.")
            return False
        confirm = input(f"Install Make using {name}? (y/n): ").strip().lower()
        if confirm != 'y':
            self._emit(callback, "Make installation cancelled.")
            return False

        if not self.executor.run(command, callback=callback):
            self._emit(callback, "⚠️ Installation failed. Try running as Administrator or install Make manually.")
            return False
        return self._make_command() is not None

    def setup_git(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Initialize or update git repository"""
        self._emit(callback, "\n📦 Setting up Git...")
        git_dir = Path(self.project_path) / '.git'

        if git_dir.exists():
            # Update existing repo
            if not self.executor.run("git fetch origin", cwd=self.project_path, callback=callback):
                return False
            return self.executor.run("git pull origin", cwd=self.project_path, callback=callback)
        else:
            # Initialize new repo
            return self.executor.run("git init", cwd=self.project_path, callback=callback)

    def setup_make(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Run Make build"""
        self._emit(callback, "\n📦 Running Make...")
        make_command = self._make_command()
        if not make_command and not self._install_make(callback):
            return False
        make_command = self._make_command()

        return self.executor.run([make_command], cwd=self.project_path, callback=callback)

    def update_packages(self, callback: Optional[Callable[[str], None]] = None) -> bool:
        """Update all project packages"""
        self._emit(callback, "\n📦 Updating packages...")
        detector = ProjectDetector(self.project_path)
        detected = detector.detect()

        success = True

        if 'python' in detected:
            self._emit(callback, "\n🐍 Updating Python packages...")
            requirements = Path(self.project_path) / 'requirements.txt'
            venv_python = self._venv_python()
            if requirements.exists() and venv_python is not None:
                success &= self.executor.run(
                    [str(venv_python), '-m', 'pip', 'install', '--upgrade', 'pip'],
                    cwd=self.project_path, callback=callback)
                success &= self._pip_install(venv_python, ['-r', 'requirements.txt'], callback)
            else:
                self._emit(callback, "   ⚪ Skipping Python updates: create the venv and requirements.txt first")
        if 'nodejs' in detected:
            self._emit(callback, "\n🟢 Updating Node.js packages...")
            project_root = Path(self.project_path)
            if (project_root / 'yarn.lock').exists():
                success &= self.executor.run("yarn upgrade", cwd=self.project_path, callback=callback)
            elif (project_root / 'pnpm-lock.yaml').exists():
                success &= self.executor.run("pnpm update", cwd=self.project_path, callback=callback)
            else:
                success &= self.executor.run("npm update", cwd=self.project_path, callback=callback)

        if 'dotnet' in detected:
            self._emit(callback, "\n🔷 Updating .NET packages...")
            success &= self.executor.run("dotnet restore --no-cache", cwd=self.project_path, callback=callback)

        return success
