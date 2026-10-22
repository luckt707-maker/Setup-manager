from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


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
    """Registry of QoL extensions for different project types"""

    EXTENSIONS = {
        'python': {
            'linters': [
                {'name': 'pylint', 'cmd': 'pip install pylint', 'run': 'pylint .', 'desc': 'Code quality checker'},
                {'name': 'flake8', 'cmd': 'pip install flake8', 'run': 'flake8 .', 'desc': 'Style guide enforcement'},
                {'name': 'black', 'cmd': 'pip install black', 'run': 'black .', 'desc': 'Code formatter'},
            ],
            'type_checkers': [
                {'name': 'mypy', 'cmd': 'pip install mypy', 'run': 'mypy .', 'desc': 'Static type checker'},
            ],
            'testers': [
                {'name': 'pytest', 'cmd': 'pip install pytest pytest-cov', 'run': 'pytest', 'desc': 'Testing framework'},
                {'name': 'unittest', 'cmd': 'python -m unittest', 'run': 'python -m unittest discover', 'desc': 'Built-in testing'},
            ],
            'security': [
                {'name': 'bandit', 'cmd': 'pip install bandit', 'run': 'bandit -r .', 'desc': 'Security issue finder'},
            ],
            'profilers': [
                {'name': 'cProfile', 'cmd': 'python -m cProfile', 'run': 'python -m cProfile -s cumtime your_script.py', 'desc': 'Performance profiler'},
            ],
            'debuggers': [
                {'name': 'pdb', 'cmd': 'python -m pdb', 'run': 'python -m pdb your_script.py', 'desc': 'Python debugger'},
                {'name': 'ipdb', 'cmd': 'pip install ipdb', 'run': 'ipdb your_script.py', 'desc': 'Enhanced debugger'},
            ],
        },
        'nodejs': {
            'linters': [
                {'name': 'eslint', 'cmd': 'npm install --save-dev eslint', 'run': 'npx eslint .', 'desc': 'JS linter'},
                {'name': 'prettier', 'cmd': 'npm install --save-dev prettier', 'run': 'npx prettier --write .', 'desc': 'Code formatter'},
            ],
            'type_checkers': [
                {'name': 'TypeScript', 'cmd': 'npm install --save-dev typescript', 'run': 'npx tsc --noEmit', 'desc': 'Type checking'},
            ],
            'testers': [
                {'name': 'Jest', 'cmd': 'npm install --save-dev jest', 'run': 'npm test', 'desc': 'Testing framework'},
                {'name': 'Mocha', 'cmd': 'npm install --save-dev mocha chai', 'run': 'npx mocha', 'desc': 'Testing framework'},
            ],
            'security': [
                {'name': 'npm audit', 'cmd': 'npm audit', 'run': 'npm audit', 'desc': 'Dependency vulnerabilities'},
            ],
            'profilers': [
                {'name': 'Node profiler', 'cmd': 'node --prof', 'run': 'node --prof your_script.js', 'desc': 'Performance profiler'},
            ],
            'debuggers': [
                {'name': 'Node debugger', 'cmd': 'node --inspect', 'run': 'node --inspect your_script.js', 'desc': 'Node debugger'},
            ],
        },
        'dotnet': {
            'linters': [
                {'name': 'StyleCop', 'cmd': 'dotnet add package StyleCop.Analyzers', 'run': 'dotnet build', 'desc': 'Code style analyzer'},
            ],
            'type_checkers': [
                {'name': 'Roslyn', 'cmd': 'dotnet build', 'run': 'dotnet build', 'desc': 'Type checking'},
            ],
            'testers': [
                {'name': 'xUnit', 'cmd': 'dotnet add package xunit', 'run': 'dotnet test', 'desc': 'Testing framework'},
                {'name': 'NUnit', 'cmd': 'dotnet add package NUnit', 'run': 'dotnet test', 'desc': 'Testing framework'},
            ],
            'security': [
                {'name': 'Security Analyzer', 'cmd': 'dotnet add package Security.Analyzers', 'run': 'dotnet build', 'desc': 'Security checks'},
            ],
            'debuggers': [
                {'name': 'Visual Studio Debugger', 'cmd': 'dotnet build -c Debug', 'run': 'dotnet run --configuration Debug', 'desc': '.NET debugger'},
            ],
        }
    }

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
    """Detect project types based on files in directory"""

    DETECTORS = {
        'python': ['requirements.txt', 'setup.py', 'pyproject.toml', 'Pipfile', '*.py'],
        'nodejs': ['package.json', 'yarn.lock', 'pnpm-lock.yaml', '*.js', '*.ts'],
        'dotnet': ['*.csproj', '*.sln', '.csproj', '*.cs'],
        'make': ['Makefile', 'makefile', 'CMakeLists.txt'],
        'git': ['.git'],
        'docker': ['Dockerfile', 'docker-compose.yml', 'docker-compose.yaml'],
        'ruby': ['Gemfile'],
    }

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
