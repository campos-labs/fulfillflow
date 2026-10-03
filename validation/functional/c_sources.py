"""Offline inventory of frozen C sources; never imports or executes application code."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

REFERENCES = {
    "v1.0.0": "6235f6cb2a733e23ea76cf8264d2145f3759a871",
    "v1.1.0-rc.1": "217e29a230689da3bd6359790f0753b41a10a927",
    "v1.2.0-rc.1": "9b445f9b5466cd302c89f1deed7a9c051cb397ae",
}
REQUIRED = ("DESIGN.md", "uv.lock", "pyproject.toml", "src/fulfillflow/tracking/service.py")


def git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, timeout=30, check=False
    )
    if result.returncode:
        raise ValueError("FROZEN_GIT_READ_FAILED")
    return result.stdout


def inventory(repo: Path, references: dict[str, str]) -> dict[str, Any]:
    """Read immutable blobs, including distinct locks, independently of current checkout."""
    if not references:
        raise ValueError("EMPTY_REFERENCES")
    versions: dict[str, Any] = {}
    for tag, expected in references.items():
        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?", tag):
            raise ValueError("INVALID_REFERENCE")
        if not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise ValueError("INVALID_SHA")
        actual = git_bytes(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
        if actual.decode("ascii").strip() != expected:
            raise ValueError("FROZEN_TAG_MISMATCH")
        paths = git_bytes(repo, "ls-tree", "-r", "--name-only", expected).decode().splitlines()
        if not set(REQUIRED).issubset(paths):
            raise ValueError("REQUIRED_SOURCE_MISSING")
        selected = [p for p in paths if p in REQUIRED or p.startswith(("src/", "alembic"))]
        hashes = {
            p: hashlib.sha256(git_bytes(repo, "show", f"{expected}:{p}")).hexdigest()
            for p in selected
        }
        versions[tag] = {"sha": expected, "git_blob_sha256": hashes}
    return {
        "schema_version": 1,
        "status": "SOURCE_INVENTORY_ONLY",
        "evaluated_execution_authorized": False,
        "versions": versions,
        "limits": [
            "Git blobs only; not checkout bytes or runtime provenance.",
            "No dependency installation, application import, database or scenario execution.",
            "Runtime/transport/barrier qualification remains required before evaluated runs.",
        ],
    }


def write_new(path: Path, data: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_new(args.output, inventory(args.repository, REFERENCES))


if __name__ == "__main__":
    main()
