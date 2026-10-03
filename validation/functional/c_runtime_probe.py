"""Check a C runtime in its own interpreter before importing its frozen application."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from validation.functional.c_runtime import save_record, verify_export
from validation.functional.c_sources import REFERENCES
from validation.functional.provenance import _name, _production_closure


def capture(record: dict[str, Any]) -> dict[str, Any]:
    if REFERENCES.get(record["tag"]) != record["sha"]:
        raise ValueError("FROZEN_TAG_MISMATCH")
    verify_export(record)
    if sys.version_info[:2] != (3, 13):
        raise ValueError("PYTHON_313_REQUIRED")
    source = Path(record["source"]).resolve()
    lock = tomllib.loads((source / "uv.lock").read_text(encoding="utf-8"))
    locked: dict[str, set[str]] = {}
    for package in lock["package"]:
        locked.setdefault(_name(package["name"]), set()).add(package["version"])
    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = _name(distribution.metadata["Name"])
        if name in installed:
            raise ValueError("DUPLICATE_INSTALLED_DISTRIBUTION")
        if distribution.version not in locked.get(name, set()):
            raise ValueError("INSTALLED_VERSION_DIFFERS_FROM_LOCK")
        installed[name] = distribution.version
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    checked = _production_closure(project["project"]["dependencies"], installed, locked)
    import fulfillflow

    module = Path(fulfillflow.__file__).resolve()
    if module != source / "src/fulfillflow/__init__.py":
        raise ValueError("APPLICATION_IMPORT_SOURCE_MISMATCH")
    return {
        "status": "RUNTIME_IDENTITY_VERIFIED_NOT_SCENARIO_EXECUTION",
        "application_sha": record["sha"],
        "tag": record["tag"],
        "source": str(source),
        "module": str(module),
        "python": sys.version,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "lock_sha256": hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest(),
        "installed": installed,
        "production_dependencies_checked": checked,
        "source_files_verified": len(record["files_sha256"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-record", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    record = json.loads(args.source_record.read_text(encoding="utf-8"))
    save_record(args.output, capture(record))


if __name__ == "__main__":
    main()
