from pathlib import Path
from typing import Any, Dict, List
import ast
import os
import re


class SecurityScanner:
    """Perform bounded, read-only checks for common risky source patterns."""

    SKIP_DIRS = {
        '.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__',
        '.tox', '.mypy_cache', '.pytest_cache', 'build', 'dist', 'vendor',
    }
    SOURCE_EXTENSIONS = {'.py', '.js', '.jsx', '.ts', '.tsx', '.cs'}
    MAX_DEPTH = 6
    MAX_FILES = 1000
    MAX_FILE_BYTES = 50 * 1024
    SECRET_PATTERN = re.compile(
        r'''(?i)\b(api[_-]?key|client[_-]?secret|password|access[_-]?token)\b\s*[:=]\s*["']([^"'\s]{8,})["']'''
    )
    # A private key pasted into a source file is a finding of its own, and
    # none of the patterns above match the PEM armour around it.
    PRIVATE_KEY_PATTERN = re.compile(
        r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----'
        r'|-----BEGIN CERTIFICATE-----'
    )
    JS_RULES = (
        (re.compile(r'\beval\s*\('), 'SEC001', 'medium', 'Dynamic code execution with eval().'),
        (re.compile(r'\bnew\s+Function\s*\('), 'SEC002', 'medium', 'Dynamic code construction with Function().'),
        (re.compile(r'\bchild_process\s*\.\s*exec(?:Sync)?\s*\('), 'SEC003', 'high', 'Shell command execution; validate all inputs.'),
        (re.compile(r'dangerouslySetInnerHTML'), 'SEC004', 'medium', 'Raw HTML insertion can enable cross-site scripting.'),
    )

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = SecurityScanner._call_name(node.value)
            return f'{prefix}.{node.attr}' if prefix else node.attr
        return ''

    @classmethod
    def _python_findings(cls, source: str, relative_path: str) -> List[Dict[str, str]]:
        findings = []
        try:
            tree = ast.parse(source, filename=relative_path)
        except SyntaxError:
            return findings

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = cls._call_name(node.func)
            rule = None
            severity = 'medium'
            message = ''

            if call_name in {'eval', 'exec'}:
                rule, message = 'SEC001', 'Dynamic code execution with eval()/exec().' 
            elif call_name == 'os.system':
                dynamic_command = not node.args or not isinstance(node.args[0], ast.Constant)
                if dynamic_command:
                    rule, severity, message = 'SEC003', 'high', 'Shell command execution uses a dynamic value; validate all inputs.'
            elif call_name in {'pickle.load', 'pickle.loads', 'dill.load', 'dill.loads'}:
                rule, severity, message = 'SEC005', 'high', 'Deserializing untrusted data can execute arbitrary code.'
            elif call_name == 'yaml.load' and not any(
                keyword.arg == 'Loader'
                and isinstance(keyword.value, ast.Attribute)
                and keyword.value.attr in {'SafeLoader', 'CSafeLoader'}
                for keyword in node.keywords
            ):
                rule, severity, message = 'SEC006', 'high', 'yaml.load() may construct unsafe Python objects; prefer safe_load().'
            elif call_name.startswith('subprocess.') and any(
                keyword.arg == 'shell'
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                rule, severity, message = 'SEC007', 'high', 'subprocess with shell=True can enable command injection.'

            if rule:
                findings.append({
                    'file': relative_path,
                    'line': str(node.lineno),
                    'rule': rule,
                    'severity': severity,
                    'message': message,
                })
        return findings

    @classmethod
    def scan(cls, root: Path) -> Dict[str, Any]:
        findings: List[Dict[str, str]] = []
        files_scanned = 0
        truncated = False

        for current, directories, filenames in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            directories[:] = [
                name for name in directories
                if name.lower() not in cls.SKIP_DIRS and not name.startswith('.')
            ]
            if depth >= cls.MAX_DEPTH:
                directories.clear()

            for filename in filenames:
                path = current_path / filename
                if path.suffix.lower() not in cls.SOURCE_EXTENSIONS:
                    continue
                if files_scanned >= cls.MAX_FILES:
                    truncated = True
                    break
                try:
                    with path.open('rb') as source_file:
                        content = source_file.read(cls.MAX_FILE_BYTES + 1)
                    if len(content) > cls.MAX_FILE_BYTES or b'\0' in content:
                        continue
                    source = content.decode('utf-8', errors='replace')
                except OSError:
                    continue

                files_scanned += 1
                relative_path = path.relative_to(root).as_posix()
                if path.suffix.lower() == '.py':
                    findings.extend(cls._python_findings(source, relative_path))
                else:
                    for line_number, line in enumerate(source.splitlines(), 1):
                        for pattern, rule, severity, message in cls.JS_RULES:
                            if pattern.search(line):
                                findings.append({
                                    'file': relative_path,
                                    'line': str(line_number),
                                    'rule': rule,
                                    'severity': severity,
                                    'message': message,
                                })

                for line_number, line in enumerate(source.splitlines(), 1):
                    if cls.SECRET_PATTERN.search(line):
                        findings.append({
                            'file': relative_path,
                            'line': str(line_number),
                            'rule': 'SEC008',
                            'severity': 'high',
                            'message': 'Possible hard-coded credential; rotate it if it is real and load it from a secret store.'
                        })
                    elif cls.PRIVATE_KEY_PATTERN.search(line):
                        findings.append({
                            'file': relative_path,
                            'line': str(line_number),
                            'rule': 'SEC009',
                            'severity': 'critical',
                            'message': 'Private key material committed to the source tree; revoke and replace it.'
                        })
            if truncated:
                break

        return {
            'files_scanned': files_scanned,
            'truncated': truncated,
            'scope': 'Heuristic source scan only; no code execution, exploit testing, or dependency CVE audit.',
            'findings': findings,
        }
