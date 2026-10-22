from pathlib import Path
from typing import Any, Dict, List
import json
import os
import shutil

from .dependencies import (
    declared_requirements,
    find_venv,
    installed_distributions,
    normalize_name,
    site_packages_dirs,
)
from .detector import ProjectDetector
from .security import SecurityScanner
from .setup import Setup
from .tooling import compose_services, make_command, make_targets


class ProjectOrchestrator:
    """Headless orchestration logic for Setup Manager"""

    MAX_DISCOVERED_PROJECTS = 200

    def __init__(self, folder_path: str = '.'):
        self.folder_path = Path(folder_path).resolve()
        self.setup = Setup(str(self.folder_path))
        self.detector = ProjectDetector(str(self.folder_path))
        self.detected_types = self.detector.detect()

    def analyze(self) -> Dict:
        """Return analysis results as a dictionary"""
        candidates = self._get_launch_candidates()
        return {
            "folder": str(self.folder_path),
            "detected": self.detected_types,
            "candidates": candidates,
            "projects": self._discover_projects(),
        }

    def diagnose(self) -> Dict[str, Any]:
        """Report runtime and dependency requirements without running project code."""
        checks = []
        install_commands = []

        def check_runtime(name: str, command: str, download_url: str) -> bool:
            available = shutil.which(command) is not None
            checks.append({
                'name': name,
                'status': 'ready' if available else 'missing',
                'detail': f'{command} is available' if available else f'{command} was not found on PATH',
                'download_url': None if available else download_url,
            })
            return available

        if 'python' in self.detected_types:
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
                checks.append({
                    'name': 'Python dependencies',
                    'status': 'missing' if missing else 'ready',
                    'detail': (
                        f'Missing: {", ".join(missing)}' if missing
                        else f'{len(declared.requirements)} declared package(s) are installed'
                    ),
                    'download_url': None,
                })
                if missing:
                    requirements = self.setup.python_install_requirements()
                    install_commands.append({
                        'label': 'Install Python dependencies',
                        'type': 'python',
                        'command': 'python -m pip install ' + ' '.join(requirements),
                    })

        if 'nodejs' in self.detected_types:
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

        if 'dotnet' in self.detected_types:
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

        return {
            'folder': str(self.folder_path),
            'detected': self.detected_types,
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

        if 'python' in self.detected_types:
            python_files = sorted(
                path for path in self.folder_path.glob('*.py')
                if path.name not in {'__init__.py', 'setup.py'}
            )
            for path in python_files[:10]:
                candidates.append({
                    'label': f'Python: {path.name}',
                    'command': ['python', path.name],
                })

        package_file = self.folder_path / 'package.json'
        if 'nodejs' in self.detected_types and package_file.exists():
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

        if 'dotnet' in self.detected_types:
            candidates.append({
                'label': '.NET project',
                'command': ['dotnet', 'run'],
            })

        if 'make' in self.detected_types:
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

        if 'docker' in self.detected_types:
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

        return candidates
