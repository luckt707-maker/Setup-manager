from pathlib import Path
from typing import Callable, Dict, List, Optional
import subprocess
import sys

from .cmdline import split_command
from .dependencies import (
    declared_requirements,
    find_venv,
    installed_distributions,
    normalize_name,
    site_packages_dirs,
)
from .detector import ProjectDetector, QoLExtensionsRegistry
from .executor import CommandExecutor


class Setup:
    """Setup handlers for different project types"""

    QUALITY_CHECK_CATEGORIES = ('linters', 'type_checkers', 'testers', 'security')

    # Both pytest and `python -m unittest discover` exit with 5 when they
    # found no tests to run. For a project that uses a different test runner
    # that is the normal case, not a failure, so it is reported as a skip.
    NO_TESTS_EXIT_CODE = 5

    # Only these commands install something. The rest of the registry holds
    # run commands ('python -m cProfile', 'node --prof', 'npm audit'),
    # and running one of those as an installation does nothing useful and
    # can hang the task forever.
    INSTALL_PREFIXES = {
        'python': ('pip ', 'pip3 '),
        'nodejs': ('npm install ', 'npm i ', 'npx '),
        'dotnet': ('dotnet add package ',),
    }

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
        """What to give pip for this project.

        A project with a requirements.txt is installed from that file; one
        that declares its dependencies in pyproject.toml - the Poetry
        table or the standard ``[project]`` table - is given the
        requirements themselves.
        """
        project_root = Path(self.project_path).expanduser().resolve()
        if (project_root / 'requirements.txt').is_file():
            return ['-r', 'requirements.txt']
        declared = declared_requirements(project_root)
        return [requirement.to_pip() for requirement in declared.requirements]

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
        return self.executor.run(
            [str(venv_python), '-m', 'pip', 'install', '--only-binary=:all:',
             *[requirement.to_pip() for requirement in missing]],
            cwd=self.project_path,
            callback=callback,
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

        # Install requirements if exists
        req_file = Path(self.project_path) / 'requirements.txt'
        if req_file.exists():
            # --only-binary=:all: keeps pip from building a source
            # distribution, which runs the project's setup.py.
            if not self.executor.run(
                [str(venv_python), '-m', 'pip', 'install', '--only-binary=:all:', '-r', 'requirements.txt'],
                cwd=self.project_path, callback=callback):
                return False

        # A project that declares its dependencies in pyproject.toml - the
        # Poetry table or the standard [project] table - needs them
        # installed just as much as one with a requirements.txt.
        declared = declared_requirements(project_root)
        extra = [
            requirement
            for requirement in declared.requirements
            if requirement.source != 'requirements.txt'
        ]
        if extra:
            names = ", ".join(requirement.name for requirement in extra)
            self._emit(callback, f"  📦 Installing the project's dependencies: {names}")
            if not self.executor.run(
                [str(venv_python), '-m', 'pip', 'install', '--only-binary=:all:',
                 *[requirement.to_pip() for requirement in extra]],
                cwd=self.project_path, callback=callback):
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
                success &= self.executor.run(
                    [str(venv_python), '-m', 'pip', 'install', '--only-binary=:all:', '-r', 'requirements.txt'],
                    cwd=self.project_path, callback=callback)
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
