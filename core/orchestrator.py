from pathlib import Path
from typing import Any, Dict, List
import importlib.metadata
import json
import os
import re
import shutil

from .detector import ProjectDetector
from .security import SecurityScanner
from .setup import Setup


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
            requirements = self.folder_path / 'requirements.txt'
            if requirements.is_file():
                try:
                    installed = {
                        re.sub(r'[-_.]+', '-', distribution.metadata['Name']).lower()
                        for distribution in importlib.metadata.distributions()
                        if distribution.metadata.get('Name')
                    }
                    declared = set()
                    for line in requirements.read_text(encoding='utf-8').splitlines():
                        line = line.strip()
                        match = re.match(r'^([A-Za-z0-9][A-Za-z0-9._-]*)', line)
                        if match and not line.startswith(('#', '-')):
                            declared.add(match.group(1))
                    missing = sorted(
                        name for name in declared
                        if re.sub(r'[-_.]+', '-', name).lower() not in installed
                    )
                    checks.append({
                        'name': 'Python dependencies',
                        'status': 'missing' if missing else 'ready',
                        'detail': (
                            f'Missing: {", ".join(missing)}' if missing
                            else f'{len(declared)} declared package(s) are installed in the Setup Manager interpreter'
                        ),
                        'download_url': None,
                    })
                    if missing:
                        install_commands.append({
                            'label': 'Install Python requirements',
                            'type': 'python',
                            'command': 'python -m pip install -r requirements.txt',
                        })
                except (OSError, UnicodeError) as exc:
                    checks.append({
                        'name': 'Python dependencies',
                        'status': 'unknown',
                        'detail': f'Could not read requirements.txt: {exc}',
                        'download_url': None,
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
            make_command = 'make' if shutil.which('make') else 'mingw32-make'
            candidates.append({
                'label': 'Make target: default',
                'command': [make_command],
            })

        if 'docker' in self.detected_types and any(
            (self.folder_path / filename).exists()
            for filename in ('docker-compose.yml', 'docker-compose.yaml')
        ):
            candidates.append({
                'label': 'Docker Compose',
                'command': ['docker', 'compose', 'up'],
            })

        return candidates
