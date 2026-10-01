"""Independent source/tool identities; no environment or credentials are exported."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement

EXPECTED = "9b445f9b5466cd302c89f1deed7a9c051cb397ae"
_GENERATED = frozenset(
    {"results", "__pycache__", ".tmp", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".artifacts"}
)


def git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args], capture_output=True, timeout=15, check=False
    )
    if result.returncode:
        raise ValueError("SOURCE_GIT_CHECK_FAILED")
    return result.stdout.decode("utf-8").strip()


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _production_closure(
    declarations: list[str], installed: dict[str, str], locked: dict[str, set[str]]
) -> list[str]:
    # Requirements' markers are evaluated in their parent's requested extras context.
    # A package encountered later with another extra must expand that extra as well.
    pending = [(text, frozenset({""})) for text in declarations]
    expanded: dict[str, set[str]] = {}
    checked: set[str] = set()
    while pending:
        text, contexts = pending.pop()
        requirement = Requirement(text)
        if requirement.marker and not any(
            requirement.marker.evaluate({"extra": extra}) for extra in contexts
        ):
            continue
        name = _name(requirement.name)
        if name not in installed or installed[name] not in locked.get(name, set()):
            raise ValueError("PRODUCTION_DEPENDENCY_NOT_FROZEN")
        if requirement.url:
            raise ValueError("DIRECT_DEPENDENCY_REFERENCE_NOT_SUPPORTED")
        if requirement.specifier and installed[name] not in requirement.specifier:
            raise ValueError("DEPENDENCY_REQUIREMENT_NOT_MET")
        checked.add(name)
        requested = {"", *(_name(extra) for extra in requirement.extras)}
        seen = expanded.setdefault(name, set())
        fresh = requested - seen
        if not fresh:
            continue
        seen.update(fresh)
        pending.extend(
            (dependency, frozenset(fresh))
            for dependency in (importlib.metadata.requires(name) or [])
        )
    return sorted(checked)


def capture(source: Path, folder: Path) -> dict[str, Any]:
    source = source.resolve()
    folder = folder.resolve()
    if git(source, "rev-parse", "HEAD") != EXPECTED:
        raise ValueError("WRONG_APPLICATION_HEAD")
    if git(source, "rev-parse", "v1.2.0-rc.1^{commit}") != EXPECTED:
        raise ValueError("WRONG_APPLICATION_TAG")
    if git(source, "diff", "--name-only", "HEAD"):
        raise ValueError("FROZEN_TRACKED_FILES_CHANGED")
    # Include ignored Python files: an ignored module can still affect imports.
    untracked = git(source, "ls-files", "--others", "--", "src").splitlines()
    if any(Path(path).suffix.lower() in {".py", ".pyi", ".pyd", ".so"} for path in untracked):
        raise ValueError("UNTRACKED_APPLICATION_MODULE")
    if sys.version_info[:2] != (3, 13):
        raise ValueError("PYTHON_313_REQUIRED")
    import fulfillflow

    module = Path(fulfillflow.__file__).resolve()
    if not module.is_relative_to(source / "src"):
        raise ValueError("APPLICATION_IMPORT_SOURCE_MISMATCH")
    lock = tomllib.loads((source / "uv.lock").read_text(encoding="utf-8"))
    locked: dict[str, set[str]] = {}
    for package in lock["package"]:
        locked.setdefault(_name(package["name"]), set()).add(package["version"])
    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = _name(distribution.metadata["Name"])
        version = distribution.version
        if name in installed:
            raise ValueError("DUPLICATE_INSTALLED_DISTRIBUTION")
        installed[name] = version
        if name not in locked:
            raise ValueError("INSTALLED_PACKAGE_NOT_IN_LOCK")
        if version not in locked[name]:
            raise ValueError("INSTALLED_VERSION_DIFFERS_FROM_LOCK")
    root = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    checked = _production_closure(root["project"]["dependencies"], installed, locked)

    source_paths = git(
        source,
        "ls-files",
        "src",
        "alembic*",
        "infrastructure",
        "uv.lock",
        "pyproject.toml",
        "compose.yaml",
        "Dockerfile",
        "DESIGN.md",
    ).splitlines()
    hashes = {
        path: hashlib.sha256((source / path).read_bytes()).hexdigest() for path in source_paths
    }
    tool_hashes = {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
        and not _GENERATED.intersection(path.relative_to(folder).parts)
        and not any(part.startswith(".") for part in path.relative_to(folder).parts)
    }
    return {
        "application": {
            "tag": "v1.2.0-rc.1",
            "sha": EXPECTED,
            "source": str(source),
            "module": str(module),
            "files_sha256": hashes,
        },
        "tool": {
            "base_sha": EXPECTED,
            "files_sha256": tool_hashes,
            "uncommitted": True,
            "identity": hashlib.sha256(
                json.dumps(tool_hashes, sort_keys=True).encode()
            ).hexdigest(),
        },
        "runtime": {
            "executable": sys.executable,
            "base_executable": getattr(sys, "_base_executable", sys.executable),
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "installed": installed,
            "production_dependencies_checked": checked,
        },
    }
