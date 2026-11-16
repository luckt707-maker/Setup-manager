"""Missing project files, and updates that are safe to download.

The check reads the project folder. It asks the official Python and npm
registries which declared packages have a newer release. A file is saved
only when every one of these is true:

* the package is one the project already declares, by a plain name
* the version still fits that declaration
* the file is a binary wheel or an npm tarball, not a source build
* the address is HTTPS on the official host, with no other host in between
* the bytes match the checksum the registry published

Saved files are not installed and their code is not run.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from core.dependencies import (
    SKIP_DIRECTORIES,
    declared_requirements,
    find_venv,
    normalize_name,
    poetry_constraint,
    site_packages_dirs,
)
from core.tooling import find_makefile

PYPI_METADATA_HOSTS = frozenset({"pypi.org"})
PYTHON_FILE_HOSTS = frozenset({"files.pythonhosted.org"})
NPM_HOSTS = frozenset({"registry.npmjs.org"})
MAX_BYTES = 50 * 1024 * 1024
MAX_PACKAGES = 40
USER_AGENT = "SetupManager/1.0"

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.(whl|tgz)$")
_SPEC_PART = re.compile(r"^(==|!=|<=|>=|~=|<|>)([A-Za-z0-9.*+!_-]+)$")
_COPY = re.compile(r"^(?:COPY|ADD)\s+(.*)$", re.IGNORECASE)
_INCLUDE = re.compile(r"^-?(?:include|sinclude)\s+(.+)$", re.IGNORECASE)
_NPM_NAME = re.compile(r"^(@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+$")

Fetch = Callable[..., bytes]


@dataclass
class Update:
    """One declared package, and whether its file may be downloaded."""

    ecosystem: str
    name: str
    installed: Optional[str]
    version: Optional[str]
    safe: bool
    reason: str
    kind: str
    url: Optional[str] = None
    sha256: Optional[str] = None
    integrity: Optional[str] = None
    filename: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ecosystem": self.ecosystem,
            "name": self.name,
            "installed": self.installed,
            "version": self.version,
            "safe": self.safe,
            "reason": self.reason,
            "kind": self.kind,
            "url": self.url if self.safe else None,
            "filename": self.filename if self.safe else None,
        }


def _url_allowed(url: str, hosts: Iterable[str]) -> bool:
    """True when the URL is HTTPS on one of the named hosts and has no login in it."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    allowed = {item.lower() for item in hosts}
    if parts.scheme != "https" or host not in allowed:
        return False
    if parts.username or parts.password:
        return False
    if any(piece == ".." for piece in parts.path.split("/")):
        return False
    return True


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Stop a registry reply from sending the download somewhere else."""

    def __init__(self, hosts: Iterable[str]):
        super().__init__()
        self.hosts = {item.lower() for item in hosts}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_allowed(newurl, self.hosts):
            raise urllib.error.URLError(f"refusing redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, hosts: Iterable[str], limit: int = MAX_BYTES, timeout: float = 20) -> bytes:
    """Read one HTTPS URL from an official host. Anything else is refused."""
    if not _url_allowed(url, hosts):
        raise ValueError("the address is not the official registry")
    opener = urllib.request.build_opener(
        _RefuseRedirect(hosts),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=timeout) as response:
            if not _url_allowed(response.geturl(), hosts):
                raise ValueError("the registry sent the download to another address")
            chunks: List[bytes] = []
            total = 0
            while True:
                block = response.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise ValueError("the file is larger than 50 MB")
                chunks.append(block)
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"the registry returned {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"the registry could not be reached ({exc.reason})") from exc


def release_key(version: str) -> Optional[tuple]:
    """A plain numbered release, such as 1.2.3. Pre-releases are left out."""
    text = str(version).strip()
    if text[:1].lower() == "v" and text[1:2].isdigit():
        text = text[1:]
    text = text.split("+", 1)[0]
    if "!" in text:
        epoch, _, text = text.partition("!")
        if not epoch.isdigit():
            return None
    if re.search(r"[A-Za-z]", text):
        return None
    parts = text.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _padded(left: Sequence[int], right: Sequence[int]) -> tuple:
    width = max(len(left), len(right))
    return (
        tuple(left) + (0,) * (width - len(left)),
        tuple(right) + (0,) * (width - len(right)),
    )


def specifier_allows(spec: str, version: str) -> bool:
    """Whether a pip-style specifier still accepts this plain version."""
    text = (spec or "").split(";", 1)[0].replace(" ", "")
    key = release_key(version)
    if key is None:
        return False
    if not text:
        return True
    for part in text.split(","):
        if not part:
            continue
        match = _SPEC_PART.match(part)
        if match is None:
            return False
        op, raw = match.group(1), match.group(2)
        if op == "==" and raw.endswith(".*"):
            prefix = release_key(raw[:-2])
            if prefix is None or _padded(key, prefix)[0][: len(prefix)] != prefix:
                return False
            continue
        other = release_key(raw)
        if other is None:
            return False
        got, want = _padded(key, other)
        if op == "==" and got != want:
            return False
        if op == "!=" and got == want:
            return False
        if op == ">=" and got < want:
            return False
        if op == "<=" and got > want:
            return False
        if op == ">" and got <= want:
            return False
        if op == "<" and got >= want:
            return False
        if op == "~=" and (
            len(other) < 2 or got < want or got[: len(other) - 1] != want[: len(other) - 1]
        ):
            return False
    return True


def _newer(candidate: str, installed: Optional[str]) -> bool:
    if not installed:
        return True
    new, old = release_key(candidate), release_key(installed)
    if new is None or old is None:
        return False
    return _padded(new, old)[0] > _padded(new, old)[1]


def _platform_tag() -> str:
    if sys.platform == "win32":
        return "win_amd64" if sys.maxsize > 2**32 else "win32"
    if sys.platform == "darwin":
        return "macosx"
    return "manylinux"


def _wheel_ok(filename: str) -> bool:
    name = filename.lower()
    if not name.endswith(".whl"):
        return False
    if "py3-none-any" in name or "py2.py3-none-any" in name:
        return True
    return _platform_tag() in name and ("cp3" in name or "py3" in name)


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")
    return cleaned


def _held(ecosystem: str, name: str, installed: Optional[str], reason: str, kind: str = "held") -> Update:
    return Update(ecosystem, name, installed, None, False, reason, kind)


def _read_text(path: Path) -> str:
    """Read a project file. A Windows editor's leading BOM is ignored."""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _add_missing(found: List[Dict[str, str]], kind: str, path: str, reason: str) -> None:
    if len(found) >= 100:
        return
    if any(item["path"] == path and item["kind"] == kind for item in found):
        return
    found.append({"kind": kind, "path": path, "reason": reason})


def _requirement_includes(root: Path, found: List[Dict[str, str]]) -> None:
    for manifest in ("requirements.txt", "requirements-dev.txt"):
        file = root / manifest
        if not file.is_file():
            continue
        try:
            lines = _read_text(file).splitlines()
        except OSError:
            continue
        for line in lines:
            text = line.split("#", 1)[0].strip()
            included = ""
            for flag in ("--requirement", "--constraint", "-r", "-c"):
                if text.startswith(flag + "="):
                    included = text.split("=", 1)[1].strip().strip("\"'")
                    break
                if text.startswith(flag + " "):
                    included = text.split(None, 1)[1].strip().strip("\"'")
                    break
            if not included or "://" in included or any(mark in included for mark in "*?$"):
                continue
            candidate = (file.parent / included)
            if not _inside(root, candidate):
                _add_missing(found, "file", included, f"included from {manifest} and points outside the project")
            elif not candidate.is_file():
                _add_missing(found, "file", included, f"included from {manifest}, but the file is not there")


def _readme(root: Path, found: List[Dict[str, str]]) -> None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return
    try:
        import tomllib
    except ImportError:
        return
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return
    readme = (data.get("project") or {}).get("readme") if isinstance(data, dict) else None
    name = readme.get("file") if isinstance(readme, dict) else readme
    if not isinstance(name, str) or any(mark in name for mark in "*?$"):
        return
    candidate = root / name
    if not _inside(root, candidate) or not candidate.is_file():
        _add_missing(found, "file", name, "named as the readme in pyproject.toml")


def _docker_sources(root: Path, found: List[Dict[str, str]]) -> None:
    dockerfile = root / "Dockerfile"
    if not dockerfile.is_file():
        return
    try:
        lines = _read_text(dockerfile).splitlines()
    except OSError:
        return
    for line in lines:
        match = _COPY.match(line.strip())
        if match is None:
            continue
        rest = match.group(1).strip()
        if rest.upper().startswith("--FROM"):
            continue
        if rest.startswith("["):
            try:
                parts = json.loads(rest)
            except ValueError:
                continue
            sources = [str(part) for part in parts[:-1]] if isinstance(parts, list) else []
        else:
            tokens = [token for token in rest.split() if not token.startswith("--")]
            sources = tokens[:-1]
        for source in sources:
            if "://" in source or any(mark in source for mark in "*?$"):
                continue
            candidate = root / source
            kind = "folder" if source.endswith("/") else "file"
            if not _inside(root, candidate):
                _add_missing(found, kind, source, "copied by the Dockerfile and points outside the project")
            elif not candidate.exists():
                _add_missing(found, kind, source, "copied by the Dockerfile, but it is not there")


def _makefile_includes(root: Path, found: List[Dict[str, str]]) -> None:
    makefile = find_makefile(root)
    if makefile is None:
        return
    try:
        lines = _read_text(makefile).splitlines()
    except OSError:
        return
    for line in lines:
        match = _INCLUDE.match(line.strip())
        if match is None:
            continue
        for token in match.group(1).split():
            if "$" in token or token.startswith("#"):
                continue
            candidate = root / token
            if not _inside(root, candidate):
                _add_missing(found, "file", token, "included by the Makefile and points outside the project")
            elif not candidate.is_file():
                _add_missing(found, "file", token, "included by the Makefile, but the file is not there")


def _package_entry(root: Path, value: Any, reason: str, found: List[Dict[str, str]]) -> None:
    if not isinstance(value, str) or "://" in value or any(mark in value for mark in "*?$"):
        return
    candidate = root / value
    if not candidate.is_file() or not _inside(root, candidate):
        _add_missing(found, "file", value, reason)


def _node_entries(root: Path, found: List[Dict[str, str]]) -> None:
    manifest = root / "package.json"
    if not manifest.is_file():
        return
    try:
        data = json.loads(_read_text(manifest))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if not (root / "node_modules").is_dir():
        _add_missing(found, "folder", "node_modules", "package.json is here, but node_modules is not")
    for field_name in ("main", "module", "types", "typings"):
        _package_entry(root, data.get(field_name), f"named by package.json {field_name}, but the file is not there", found)
    binary = data.get("bin")
    if isinstance(binary, str):
        _package_entry(root, binary, "named by package.json bin, but the file is not there", found)
    elif isinstance(binary, dict):
        for target in binary.values():
            _package_entry(root, target, "named by package.json bin, but the file is not there", found)


def _python_env(root: Path, found: List[Dict[str, str]]) -> None:
    if not (root / "requirements.txt").is_file() and not (root / "pyproject.toml").is_file():
        return
    if find_venv(root) is None:
        _add_missing(found, "folder", "venv", "this Python project has no virtual environment folder")


def _broken_imports(root: Path, found: List[Dict[str, str]]) -> None:
    """Relative imports whose files are not in the tree.

    ``from . import gone`` names the file in the import list, not in the
    module field, so both places are checked.
    """
    for path in root.rglob("*.py"):
        if set(path.relative_to(root).parts[:-1]) & SKIP_DIRECTORIES:
            continue
        try:
            tree = ast.parse(_read_text(path), filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            base = path.parent
            for _ in range(node.level - 1):
                base = base.parent
            names = [node.module] if node.module else [
                alias.name for alias in node.names if alias.name != "*"
            ]
            for name in names:
                if not name:
                    continue
                candidate = base.joinpath(*name.split("."))
                if candidate.with_suffix(".py").is_file() or (candidate / "__init__.py").is_file():
                    continue
                where = f"{path.name} line {getattr(node, 'lineno', 0)}"
                if not _inside(root, candidate):
                    _add_missing(found, "file", name, f"{where}: the import points outside the project")
                else:
                    _add_missing(found, "file", name, f"{where}: the file it points at is not in the project")


def missing_paths(root: Path) -> List[Dict[str, str]]:
    """Files and folders the project points at that are not there.

    Nothing is downloaded. A missing source file stays a report.
    """
    root = Path(root)
    found: List[Dict[str, str]] = []
    _requirement_includes(root, found)
    _readme(root, found)
    _docker_sources(root, found)
    _makefile_includes(root, found)
    _node_entries(root, found)
    _python_env(root, found)
    _broken_imports(root, found)
    return found


def _installed_python_version(root: Path, name: str) -> Optional[str]:
    target = normalize_name(name)
    for directory in site_packages_dirs(find_venv(root)):
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".dist-info"):
                continue
            stem = entry.name[: -len(".dist-info")]
            if "-" not in stem:
                continue
            dist, folder_version = stem.rsplit("-", 1)
            if normalize_name(dist) != target:
                continue
            meta = entry / "METADATA"
            try:
                lines = _read_text(meta).splitlines()
            except OSError:
                return folder_version
            for line in lines:
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip()
            return folder_version
    return None


def _node_manifest(root: Path) -> Dict[str, str]:
    """Declared npm names and the version range written next to each one."""
    manifest = root / "package.json"
    if not manifest.is_file():
        return {}
    try:
        data = json.loads(_read_text(manifest))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    declared: Dict[str, str] = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        entries = data.get(section)
        if not isinstance(entries, dict):
            continue
        for name, spec in entries.items():
            if name not in declared and isinstance(spec, str):
                declared[str(name)] = spec
    return declared


def _plain_npm_name(name: str) -> bool:
    """A registry package name, with no path segments hiding in it."""
    if _NPM_NAME.fullmatch(name) is None:
        return False
    return all(part not in {".", ".."} for part in name.split("/"))


def _installed_node_version(root: Path, name: str) -> Optional[str]:
    if not _plain_npm_name(name):
        return None
    folder = root / "node_modules" / Path(*name.split("/"))
    manifest = folder / "package.json"
    try:
        data = json.loads(_read_text(manifest))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return str(version) if isinstance(version, str) else None


def _load_json(url: str, hosts: Iterable[str], fetch: Fetch) -> Dict[str, Any]:
    payload = fetch(url, hosts)
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("the registry reply could not be read")
    return data


def _pick_wheel(files: Any) -> Optional[Dict[str, str]]:
    chosen: Optional[tuple] = None
    if not isinstance(files, list):
        return None
    for item in files:
        if not isinstance(item, dict) or item.get("yanked"):
            continue
        if item.get("packagetype") != "bdist_wheel":
            continue
        filename = str(item.get("filename") or "")
        url = str(item.get("url") or "")
        digest = str((item.get("digests") or {}).get("sha256") or "")
        if not _wheel_ok(filename) or not _SHA256.fullmatch(digest) or not _url_allowed(url, PYTHON_FILE_HOSTS):
            continue
        rank = 0 if "none-any" in filename.lower() else 1
        if chosen is None or rank < chosen[0]:
            chosen = (rank, url, digest, filename)
    if chosen is None:
        return None
    _, url, digest, filename = chosen
    safe_name = filename if _ARCHIVE_NAME.fullmatch(filename) else None
    return {"url": url, "sha256": digest, "filename": safe_name or ""}


def _python_updates(root: Path, fetch: Fetch, seen: Set[str]) -> List[Update]:
    updates: List[Update] = []
    declared = declared_requirements(root)
    for requirement in declared.requirements:
        if len(seen) >= MAX_PACKAGES:
            break
        name = requirement.name
        key = "python:" + normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        installed = _installed_python_version(root, name)
        kind = "missing" if installed is None else "update"
        try:
            data = _load_json(
                "https://pypi.org/pypi/" + urllib.parse.quote(normalize_name(name)) + "/json",
                PYPI_METADATA_HOSTS,
                fetch,
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            updates.append(_held("python", name, installed, str(exc), kind))
            continue
        info_name = str((data.get("info") or {}).get("name") or "")
        if normalize_name(info_name) != normalize_name(name):
            updates.append(_held("python", name, installed, "the registry named a different package", kind))
            continue
        best: Optional[tuple] = None
        saw_version_gap = False
        saw_wheel_gap = False
        for version, files in (data.get("releases") or {}).items():
            if not specifier_allows(requirement.specifier, str(version)):
                saw_version_gap = True
                continue
            if not _newer(str(version), installed):
                continue
            wheel = _pick_wheel(files)
            if wheel is None:
                saw_wheel_gap = True
                continue
            key_version = release_key(str(version))
            if key_version is None:
                continue
            if best is None or _padded(key_version, best[0])[0] > _padded(key_version, best[0])[1]:
                best = (key_version, str(version), wheel)
        if best is None:
            if installed is None:
                if saw_wheel_gap:
                    reason = "the newer release has no binary wheel for this computer"
                elif saw_version_gap:
                    reason = "no release matching the declared version has a binary wheel"
                else:
                    reason = "no newer binary wheel from PyPI fits this project"
                updates.append(_held("python", name, installed, reason, "missing"))
            continue
        _, version, wheel = best
        filename = wheel["filename"] or f"{_slug(name)}-{_slug(version)}.whl"
        if not filename.endswith(".whl") or not _slug(name):
            updates.append(_held("python", name, installed, "the file name is not safe to save", kind))
            continue
        updates.append(
            Update(
                "python", name, installed, version, True,
                "binary wheel from files.pythonhosted.org, checksum published by PyPI",
                kind, wheel["url"], wheel["sha256"], None, filename,
            )
        )
    return updates


def _npm_range(spec: str) -> Optional[str]:
    text = spec.strip()
    if not text or text in {"*", "latest", "x"}:
        return ""
    if text.startswith(("git+", "git:", "github:", "http:", "https:", "file:", "link:", "workspace:", "npm:")):
        return None
    if "||" in text or " - " in text:
        return None
    return poetry_constraint(text)


def _pick_tarball(meta: Dict[str, Any]) -> Optional[Dict[str, str]]:
    dist = meta.get("dist") if isinstance(meta, dict) else None
    if not isinstance(dist, dict):
        return None
    url = str(dist.get("tarball") or "")
    integrity = str(dist.get("integrity") or "")
    if not integrity.startswith("sha512-") or not _url_allowed(url, NPM_HOSTS):
        return None
    filename = Path(urllib.parse.urlsplit(url).path).name
    safe_name = filename if _ARCHIVE_NAME.fullmatch(filename) else ""
    return {"url": url, "integrity": integrity, "filename": safe_name}


def _node_updates(root: Path, fetch: Fetch, seen: Set[str]) -> List[Update]:
    updates: List[Update] = []
    for name, spec in _node_manifest(root).items():
        if len(seen) >= MAX_PACKAGES:
            break
        if not _plain_npm_name(name):
            updates.append(_held("node", name, None, "the package name is not a plain npm name"))
            continue
        key = "node:" + name
        if key in seen:
            continue
        seen.add(key)
        installed = _installed_node_version(root, name)
        kind = "missing" if installed is None else "update"
        pip_spec = _npm_range(spec)
        if pip_spec is None:
            updates.append(_held("node", name, installed, "the version is not a plain registry release", kind))
            continue
        try:
            data = _load_json(
                "https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@"),
                NPM_HOSTS,
                fetch,
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            updates.append(_held("node", name, installed, str(exc), kind))
            continue
        if str(data.get("name") or "") != name:
            updates.append(_held("node", name, installed, "the registry named a different package", kind))
            continue
        best: Optional[tuple] = None
        saw_version_gap = False
        saw_tarball_gap = False
        versions = data.get("versions") or {}
        for version, meta in versions.items():
            if not isinstance(meta, dict) or meta.get("deprecated"):
                continue
            if not specifier_allows(pip_spec, str(version)):
                saw_version_gap = True
                continue
            if not _newer(str(version), installed):
                continue
            tarball = _pick_tarball(meta)
            if tarball is None:
                saw_tarball_gap = True
                continue
            key_version = release_key(str(version))
            if key_version is None:
                continue
            if best is None or _padded(key_version, best[0])[0] > _padded(key_version, best[0])[1]:
                best = (key_version, str(version), tarball)
        if best is None:
            if installed is None:
                if saw_tarball_gap:
                    reason = "the npm release has no checksummed tarball on registry.npmjs.org"
                elif saw_version_gap:
                    reason = "no release matching the declared version has a checksummed tarball"
                else:
                    reason = "no newer npm release fits this project"
                updates.append(_held("node", name, installed, reason, "missing"))
            continue
        _, version, tarball = best
        filename = tarball["filename"] or f"{_slug(name)}-{_slug(version)}.tgz"
        if not filename.endswith(".tgz") or not _slug(name):
            updates.append(_held("node", name, installed, "the file name is not safe to save", kind))
            continue
        updates.append(
            Update(
                "node", name, installed, version, True,
                "tarball from registry.npmjs.org, sha512 checksum published by npm",
                kind, tarball["url"], None, tarball["integrity"], filename,
            )
        )
    return updates


def _gather(root: Path, fetch: Fetch) -> tuple:
    """Missing paths, and the update objects (checksums included)."""
    seen: Set[str] = set()
    updates = _python_updates(root, fetch, seen)
    updates.extend(_node_updates(root, fetch, seen))
    return missing_paths(root), updates


def check_updates(root: Path, fetch: Optional[Fetch] = None) -> List[Dict[str, Any]]:
    """Declared packages with a newer official release, or a missing declared package.

    ``fetch`` is the only way this talks to the network. Tests pass their own.
    """
    _, updates = _gather(Path(root), fetch or fetch_url)
    return [item.to_dict() for item in updates]


def check_project(root: Path, fetch: Optional[Fetch] = None) -> Dict[str, Any]:
    """Missing files and folders, plus the update decision. Nothing is saved."""
    root = Path(root)
    missing, updates = _gather(root, fetch or fetch_url)
    return {
        "path": str(root),
        "missing": missing,
        "updates": [item.to_dict() for item in updates],
        "downloads": [],
    }


def _sri_matches(blob: bytes, integrity: str) -> bool:
    algo, _, digest = integrity.partition("-")
    if algo != "sha512" or not digest:
        return False
    import base64
    try:
        expected = base64.b64decode(digest, validate=True)
    except ValueError:
        return False
    return hashlib.sha512(blob).digest() == expected


def _save_one(root: Path, item: Update, fetch: Fetch) -> Dict[str, Any]:
    result: Dict[str, Any] = {"name": item.name, "downloaded": False, "reason": item.reason}
    if not item.safe:
        return result
    hosts = PYTHON_FILE_HOSTS if item.ecosystem == "python" else NPM_HOSTS
    url = item.url or ""
    filename = item.filename or ""
    if not _url_allowed(url, hosts) or _ARCHIVE_NAME.fullmatch(filename) is None:
        result["reason"] = "the address is not the official registry"
        return result
    try:
        blob = fetch(url, hosts)
    except (ValueError, OSError) as exc:
        result["reason"] = str(exc)
        return result
    if item.ecosystem == "python":
        digest = item.sha256 or ""
        if not _SHA256.fullmatch(digest) or hashlib.sha256(blob).hexdigest() != digest:
            result["reason"] = "the checksum did not match the registry"
            return result
    elif item.ecosystem == "node":
        if not _sri_matches(blob, item.integrity or ""):
            result["reason"] = "the checksum did not match the registry"
            return result
    else:
        result["reason"] = "the package source is not supported"
        return result
    base = Path(root) / ".setup-manager"
    if base.is_symlink() or (base.exists() and not _inside(root, base)):
        result["reason"] = "the updates folder points outside the project"
        return result
    folder = base / "updates" / item.ecosystem
    if folder.is_symlink():
        result["reason"] = "the updates folder points outside the project"
        return result
    folder.mkdir(parents=True, exist_ok=True)
    if not _inside(root, folder):
        result["reason"] = "the updates folder points outside the project"
        return result
    target = folder / filename
    if target.is_symlink() or not _inside(folder, target):
        result["reason"] = "the file name is not safe to save"
        return result
    target.write_bytes(blob)
    result.update({
        "downloaded": True,
        "path": str(target),
        "reason": "checksum matched; the file was saved and not installed",
    })
    return result


def download_safe_updates(root: Path, fetch: Optional[Fetch] = None) -> Dict[str, Any]:
    """Download only the packages this check marks safe.

    The caller does not choose the URLs. Only a result this check marks
    safe is saved, and the saved file is not installed.
    """
    root = Path(root)
    reader = fetch or fetch_url
    missing, updates = _gather(root, reader)
    return {
        "path": str(root),
        "missing": missing,
        "updates": [item.to_dict() for item in updates],
        "downloads": [_save_one(root, item, reader) for item in updates],
    }


def format_report(report: Dict[str, Any]) -> str:
    """The check as plain text."""
    lines: List[str] = []
    missing = report.get("missing") or []
    lines.append(
        f"Missing files and folders ({len(missing)})" if missing else "No missing files or folders."
    )
    for item in missing:
        lines.append(f"  {item.get('kind', 'file')}  {item.get('path')}  — {item.get('reason')}")
    updates = report.get("updates") or []
    lines.append(f"Updates ({len(updates)})" if updates else "No updates.")
    for item in updates:
        current = item.get("installed") or "not installed"
        target = item.get("version") or "held back"
        mark = "safe" if item.get("safe") else "held"
        lines.append(f"  {mark}  {item.get('name')}  {current} -> {target}  — {item.get('reason')}")
    downloads = report.get("downloads") or []
    if downloads:
        lines.append("Downloads")
        for item in downloads:
            if item.get("downloaded"):
                lines.append(f"  saved  {item.get('path')}  — {item.get('reason')}")
            else:
                lines.append(f"  skipped  {item.get('name')}  — {item.get('reason')}")
        lines.append("Saved files are not installed.")
    return "\n".join(lines)
