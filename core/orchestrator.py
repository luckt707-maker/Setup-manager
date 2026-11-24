from pathlib import Path
from typing import Any, Dict, List
import json
import os
import shutil
import subprocess

from .dependencies import (
    declared_requirements,
    find_venv,
    installed_distributions,
    normalize_name,
    site_packages_dirs,
)
from .detector import ProjectDetector
from .language_templates import get_template
from .security import SecurityScanner
from .setup import Setup
from .tooling import compose_services, gradle_command, make_command, make_targets


def _toml_scripts(folder: Path) -> List[tuple]:
    """Console scripts named in pyproject.toml."""
    path = folder / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return []
    groups = []
    project = data.get("project") if isinstance(data, dict) else None
    if isinstance(project, dict) and isinstance(project.get("scripts"), dict):
        groups.append(project["scripts"])
    poetry = data.get("tool") if isinstance(data, dict) else None
    if isinstance(poetry, dict):
        poetry = poetry.get("poetry")
    if isinstance(poetry, dict) and isinstance(poetry.get("scripts"), dict):
        groups.append(poetry["scripts"])
    found = []
    for group in groups:
        for name, target in group.items():
            if isinstance(name, str) and isinstance(target, str) and ":" in target:
                found.append((name, target))
    return found


def _python_entry_commands(folder: Path) -> List[Dict]:
    """The program's own command, rather than a tutorial file sitting beside it.

    Sherlock's welcome_launcher.py only prints a banner. The search command
    is the sherlock script. With no username typed in, the button shows usage
    and exits. A real search is `sherlock NAME` from a terminal.
    """
    venv = find_venv(folder)
    python = "python"
    if venv is not None:
        interpreter = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if interpreter.is_file():
            python = str(interpreter)
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        suffix = ".exe" if os.name == "nt" else ""
        named = scripts / f"{folder.name.lower()}{suffix}"
        if named.is_file():
            return [{
                "label": f"{named.stem}: show usage",
                "command": [str(named), "--help"],
            }]
    for name, target in _toml_scripts(folder):
        module = target.split(":", 1)[0].strip().split(".", 1)[0]
        if not module:
            continue
        if (folder / module).is_dir() or (folder / f"{module}.py").is_file():
            return [{
                "label": f"{name}: show usage",
                "command": [python, "-m", module, "--help"],
            }]
    return []


_ENTRY_SKIP = {
    ".git", "venv", ".venv", "node_modules", "__pycache__", "dist", "build",
}


def _entry_spots(folder: Path) -> List[str]:
    """Files and commands that usually start the program."""
    found: List[str] = []

    def add(name: str) -> None:
        if name and name not in found:
            found.append(name)

    for name in ("main.py", "app.py", "__main__.py", "main.go"):
        if (folder / name).is_file():
            add(name)
    try:
        children = list(folder.iterdir())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir() or child.name.startswith(".") or child.name in _ENTRY_SKIP:
            continue
        if (child / "__main__.py").is_file():
            add(f"{child.name}/__main__.py")
        if (child / "main.py").is_file():
            add(f"{child.name}/main.py")
        if child.suffix == ".app":
            add(child.name)
    for script_name, _target in _toml_scripts(folder):
        add(f"the {script_name} command in pyproject.toml")
    suffix = ".exe" if os.name == "nt" else ""
    built = folder / f"{folder.name.lower()}{suffix}"
    if suffix and built.is_file():
        add(built.name)
    return found[:6]


def entry_hint(folder: Path, candidates: List[Dict]) -> str:
    """Where to look when Run starts a script instead of the program.

    A loose ``welcome_launcher.py`` is not the program. ``main.py``,
    ``__main__.py``, ``app.py``, a command in pyproject.toml, an ``.exe``,
    or an ``.app`` usually is.
    """
    command = [str(part) for part in (candidates[0]["command"] if candidates else [])]
    if not command:
        return ""
    script = ""
    if "-m" not in command and command[-1].endswith(".py"):
        script = Path(command[-1]).name
    if not script:
        return ""
    others = [
        spot for spot in _entry_spots(folder)
        if Path(spot).name != script and spot != script
    ]
    if others:
        listed = " and ".join(others) if len(others) == 1 else ", ".join(others[:-1]) + " and " + others[-1]
        return (
            f"This runs {script}. If that file is only a helper, look at {listed}."
        )
    usual = [
        name for name in (
            "main.py", "__main__.py", "app.py", "a command in pyproject.toml",
            "an .exe in this folder", "an .app on a Mac",
        )
        if name != script
    ]
    listed = ", ".join(usual[:-1]) + ", or " + usual[-1]
    return f"This runs {script}. If that file is only a helper, look for {listed}."


class ProjectOrchestrator:
    """Headless orchestration logic for Setup Manager"""

    MAX_DISCOVERED_PROJECTS = 200

    def __init__(self, folder_path: str = '.'):
        self.folder_path = Path(folder_path).resolve()
        self.setup = Setup(str(self.folder_path))
        self.detector = ProjectDetector(str(self.folder_path))
        self.detected_types = self.detector.detect()

    def types_needed(self) -> List[str]:
        """Detected types this folder actually needs in order to run.

        A ``package.json`` or a Dockerfile next to a Python program does not
        make it a Node or Docker project. Sherlock's package.json only lists
        two unused names, and the program runs from its Python environment.
        """
        from doctor.diagnostics import required_tools

        needed_tools = set(required_tools(self.folder_path))
        tool_for = {
            "python": "python",
            "nodejs": "node",
            "typescript": "node",
            "go": "go",
            "docker": "docker",
            "make": "make",
            "git": "git",
        }
        return [
            project_type for project_type in self.detected_types
            if tool_for.get(project_type) is None or tool_for[project_type] in needed_tools
        ]

    def analyze(self) -> Dict:
        """Return analysis results as a dictionary"""
        candidates = self._get_launch_candidates()
        return {
            "folder": str(self.folder_path),
            "detected": self.types_needed(),
            "candidates": candidates,
            "entry_hint": entry_hint(self.folder_path, candidates),
            "projects": self._discover_projects(),
        }

    def diagnose(self) -> Dict[str, Any]:
        """Report runtime and dependency requirements without running project code."""
        checks = []
        install_commands = []
        needed = self.types_needed()

        def check_runtime(name: str, command: str, download_url: str) -> bool:
            available = shutil.which(command) is not None
            checks.append({
                'name': name,
                'status': 'ready' if available else 'missing',
                'detail': f'{command} is available' if available else f'{command} was not found on PATH',
                'download_url': None if available else download_url,
            })
            return available

        if 'python' in needed:
            check_runtime('Python', 'python', 'https://www.python.org/downloads/')
            # The project's own environment is what matters, and its
            # dependencies can be declared in more than one place.
            declared = declared_requirements(self.folder_path)
            if declared.requirements:
                venv = find_venv(self.folder_path)
                installed = (
                    installed_distributions(site_packages_dirs(venv)) if venv else set()
                )
                missing = sorted(
                    requirement.name
                    for requirement in declared.requirements
                    if normalize_name(requirement.name) not in installed
                )
                where = f" from {venv}" if venv else ""
                checks.append({
                    'name': 'Python dependencies',
                    'status': 'missing' if missing else 'ready',
                    'detail': (
                        f'Missing{where}: {", ".join(missing)}' if missing
                        else f'{len(declared.requirements)} declared package(s) are installed'
                    ),
                    'download_url': None,
                })
                if missing:
                    requirements = self.setup.python_install_requirements()
                    python = "python"
                    if venv is not None:
                        candidate = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                        if candidate.is_file():
                            python = str(candidate)
                    install_commands.append({
                        'label': 'Install Python dependencies',
                        'type': 'python',
                        # list2cmdline quotes a path that contains spaces, such
                        # as C:\\Users\\First Last\\Projects\\app\\venv\\...
                        'command': subprocess.list2cmdline(
                            [python, '-m', 'pip', 'install', *requirements]
                        ),
                    })

        if 'nodejs' in needed or 'typescript' in needed:
            check_runtime('Node.js', 'node', 'https://nodejs.org/en/download')
            check_runtime('npm', 'npm', 'https://nodejs.org/en/download')
            package_file = self.folder_path / 'package.json'
            if package_file.is_file():
                try:
                    package = json.loads(package_file.read_text(encoding='utf-8'))
                    declared = {
                        **package.get('dependencies', {}),
                        **package.get('devDependencies', {}),
                    }
                    missing = sorted(
                        name for name in declared
                        if not (self.folder_path / 'node_modules' / name).exists()
                    )
                    checks.append({
                        'name': 'Node.js dependencies',
                        'status': 'missing' if missing else 'ready',
                        'detail': (
                            f'Missing: {", ".join(missing[:20])}' if missing
                            else f'{len(declared)} declared package(s) are present in node_modules'
                        ),
                        'download_url': None,
                    })
                    if missing or not (self.folder_path / 'node_modules').is_dir():
                        manager = (
                            'pnpm' if (self.folder_path / 'pnpm-lock.yaml').exists()
                            else 'yarn' if (self.folder_path / 'yarn.lock').exists()
                            else 'npm'
                        )
                        install_commands.append({
                            'label': f'Install Node.js dependencies ({manager})',
                            'type': 'nodejs',
                            'command': f'{manager} install',
                        })
                except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
                    checks.append({
                        'name': 'Node.js dependencies',
                        'status': 'unknown',
                        'detail': f'Could not read package.json: {exc}',
                        'download_url': None,
                    })

        if 'dotnet' in needed:
            check_runtime('.NET SDK', 'dotnet', 'https://dotnet.microsoft.com/download')
            has_project = any(self.folder_path.glob('*.csproj')) or any(self.folder_path.glob('*.sln'))
            if has_project:
                restored = (self.folder_path / 'obj' / 'project.assets.json').is_file()
                checks.append({
                    'name': '.NET dependencies',
                    'status': 'ready' if restored else 'missing',
                    'detail': 'Restore assets are present' if restored else 'NuGet packages need to be restored',
                    'download_url': None,
                })
                if not restored:
                    install_commands.append({
                        'label': 'Restore .NET dependencies',
                        'type': 'dotnet',
                        'command': 'dotnet restore',
                    })

        # Java / Kotlin / Scala – share the Java runtime check
        if any(t in needed for t in ('java', 'kotlin', 'scala')):
            check_runtime('Java', 'java', 'https://www.oracle.com/java/technologies/downloads/')
            for lang in ('java', 'kotlin', 'scala'):
                if lang not in needed:
                    continue
                tmpl = get_template(lang)
                if tmpl is None:
                    continue
                build_file = self.folder_path / (tmpl.scaffold.keys().__iter__().__next__()
                                                  if tmpl.scaffold else '')
                # Check for common build files
                has_build = False
                if lang == 'java':
                    has_build = ((self.folder_path / 'pom.xml').exists()
                                 or (self.folder_path / 'build.gradle').exists()
                                 or (self.folder_path / 'build.gradle.kts').exists())
                elif lang == 'kotlin':
                    has_build = ((self.folder_path / 'build.gradle.kts').exists()
                                 or (self.folder_path / 'build.gradle').exists())
                elif lang == 'scala':
                    has_build = (self.folder_path / 'build.sbt').exists()

                if has_build:
                    install_commands.append({
                        'label': f'Build {tmpl.display_name} project',
                        'type': lang,
                        'command': tmpl.setup_cmd,
                    })

        if 'go' in needed:
            check_runtime('Go', 'go', 'https://go.dev/dl/')
            if (self.folder_path / 'go.mod').exists():
                install_commands.append({
                    'label': 'Tidy Go modules',
                    'type': 'go',
                    'command': 'go mod tidy',
                })

        if 'rust' in needed:
            check_runtime('Rust', 'cargo', 'https://www.rust-lang.org/tools/install')
            if (self.folder_path / 'Cargo.toml').exists():
                install_commands.append({
                    'label': 'Build Rust project',
                    'type': 'rust',
                    'command': 'cargo build',
                })

        if 'php' in needed:
            check_runtime('PHP', 'php', 'https://www.php.net/downloads.php')
            check_runtime('Composer', 'composer', 'https://getcomposer.org/download/')
            if (self.folder_path / 'composer.json').exists():
                if not (self.folder_path / 'vendor').is_dir():
                    install_commands.append({
                        'label': 'Install PHP dependencies',
                        'type': 'php',
                        'command': 'composer install',
                    })

        if 'swift' in needed:
            check_runtime('Swift', 'swift', 'https://www.swift.org/download/')
            if (self.folder_path / 'Package.swift').exists():
                install_commands.append({
                    'label': 'Build Swift project',
                    'type': 'swift',
                    'command': 'swift build',
                })

        return {
            'folder': str(self.folder_path),
            'detected': needed,
            'candidates': self._get_launch_candidates(),
            'checks': checks,
            'install_commands': install_commands,
        }

    def _discover_projects(self) -> List[Dict[str, Any]]:
        """Find project roots without traversing dependency or build directories."""
        projects = []
        for current, directories, _ in os.walk(self.folder_path):
            current_path = Path(current)
            depth = len(current_path.relative_to(self.folder_path).parts)
            directories[:] = [
                name for name in directories
                if name.lower() not in SecurityScanner.SKIP_DIRS and not name.startswith('.')
            ]
            if depth >= SecurityScanner.MAX_DEPTH:
                directories.clear()

            detected = ProjectDetector(str(current_path)).detect()
            if current_path != self.folder_path:
                manifest_markers = (
                    'requirements.txt', 'pyproject.toml', 'Pipfile', 'setup.py',
                    'package.json', 'yarn.lock', 'pnpm-lock.yaml', 'Makefile',
                    'makefile', 'CMakeLists.txt', '.git', 'Dockerfile',
                    'docker-compose.yml', 'docker-compose.yaml',
                    # New languages
                    'pom.xml', 'build.gradle', 'build.gradle.kts',
                    'go.mod', 'Cargo.toml', 'composer.json',
                    'Gemfile', 'tsconfig.json', 'Package.swift',
                    'build.sbt',
                )
                has_manifest = any((current_path / marker).exists() for marker in manifest_markers)
                has_dotnet_project = any(current_path.glob('*.csproj')) or any(current_path.glob('*.sln'))
                if not has_manifest and not has_dotnet_project:
                    continue
            if detected:
                orchestrator = self if current_path == self.folder_path else ProjectOrchestrator(str(current_path))
                projects.append({
                    'path': str(current_path),
                    'detected': detected,
                    'candidates': orchestrator._get_launch_candidates(),
                })
                if len(projects) >= self.MAX_DISCOVERED_PROJECTS:
                    break
        return projects

    def _get_launch_candidates(self) -> List[Dict]:
        """Build launch candidates for a detected project"""
        candidates = []
        needed = self.types_needed()
        entry_commands = _python_entry_commands(self.folder_path)
        if entry_commands:
            candidates.extend(entry_commands)

        if 'python' in needed and not entry_commands:
            python = "python"
            venv = find_venv(self.folder_path)
            if venv is not None:
                candidate = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                if candidate.is_file():
                    python = str(candidate)
            python_files = sorted(
                path for path in self.folder_path.glob('*.py')
                if path.name not in {'__init__.py', 'setup.py'}
            )
            for path in python_files[:10]:
                candidates.append({
                    'label': f'Python: {path.name}',
                    'command': [python, path.name],
                })

        package_file = self.folder_path / 'package.json'
        if ('nodejs' in needed or 'typescript' in needed) and package_file.exists():
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
                if isinstance(main_file, str) and (self.folder_path / main_file).is_file():
                    candidates.append({
                        'label': f'Node.js entry point: {main_file}',
                        'command': ['node', main_file],
                    })
            except (OSError, json.JSONDecodeError):
                pass

        if 'dotnet' in needed:
            candidates.append({
                'label': '.NET project',
                'command': ['dotnet', 'run'],
            })

        if 'make' in needed:
            make_program = make_command()
            if make_program is not None:
                targets = make_targets(self.folder_path)
                if targets:
                    # Every target is a different thing to run, so every
                    # target gets its own button.
                    for target in targets:
                        candidates.append({
                            'label': f'Make target: {target}',
                            'command': [make_program, target],
                        })
                else:
                    candidates.append({
                        'label': 'Make target: default',
                        'command': [make_program],
                    })

        if 'docker' in needed:
            services = compose_services(self.folder_path)
            if services:
                for service in services:
                    candidates.append({
                        'label': f'Docker service: {service}',
                        'command': ['docker', 'compose', 'up', service],
                    })
            elif (self.folder_path / 'Dockerfile').is_file():
                candidates.append({
                    'label': 'Docker: build image',
                    'command': ['docker', 'build', '.'],
                })

        if 'go' in needed:
            built_name = self.folder_path.name.lower() + (".exe" if os.name == "nt" else "")
            built = self.folder_path / built_name
            if built.is_file():
                candidates.append({
                    'label': f'Program: {built.name}',
                    'command': [str(built)],
                })
            else:
                go_files = sorted(
                    path for path in self.folder_path.glob('*.go')
                    if path.name == 'main.go'
                )
                for path in go_files[:5]:
                    candidates.append({
                        'label': f'Go: {path.name}',
                        'command': ['go', 'run', path.name],
                    })
                if not go_files:
                    candidates.append({
                        'label': 'Go project',
                        'command': ['go', 'run', '.'],
                    })

        if 'rust' in needed:
            candidates.append({
                'label': 'Rust project',
                'command': ['cargo', 'run'],
            })

        if 'java' in needed:
            if (self.folder_path / 'pom.xml').exists():
                candidates.append({
                    'label': 'Java (Maven)',
                    'command': ['mvn', 'exec:java'],
                })
            elif (self.folder_path / 'build.gradle.kts').exists() or (self.folder_path / 'build.gradle').exists():
                candidates.append({
                    'label': 'Java (Gradle)',
                    'command': gradle_command(self.folder_path, 'run'),
                })

        if 'kotlin' in needed:
            candidates.append({
                'label': 'Kotlin project',
                'command': gradle_command(self.folder_path, 'run'),
            })

        if 'scala' in needed:
            candidates.append({
                'label': 'Scala project',
                'command': ['sbt', 'run'],
            })

        if 'php' in needed:
            if (self.folder_path / 'artisan').exists():
                candidates.append({
                    'label': 'PHP (Laravel)',
                    'command': ['php', 'artisan', 'serve'],
                })
            else:
                candidates.append({
                    'label': 'PHP built-in server',
                    'command': ['php', '-S', 'localhost:8000'],
                })

        if 'swift' in needed:
            candidates.append({
                'label': 'Swift project',
                'command': ['swift', 'run'],
            })

        return candidates
