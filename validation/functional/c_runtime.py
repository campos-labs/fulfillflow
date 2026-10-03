"""Prepare isolated frozen source exports; never change the Git checkout or install tools."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from validation.functional.c_sources import REFERENCES, git_bytes


def export_source(repo: Path, tag: str, destination: Path) -> dict[str, Any]:
    if tag not in REFERENCES:
        raise ValueError("UNKNOWN_APPLICATION_REFERENCE")
    sha = REFERENCES[tag]
    actual = git_bytes(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    if actual.decode("ascii").strip() != sha:
        raise ValueError("FROZEN_TAG_MISMATCH")
    archive = git_bytes(repo, "archive", "--format=zip", sha)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        records = []
        for item in z.infolist():
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in item.filename
                or ":" in item.filename
                or stat.S_ISLNK(mode)
            ):
                raise ValueError("UNSAFE_SOURCE_ARCHIVE")
            if not item.is_dir():
                records.append((path, z.read(item)))
        # No partial checkout exists until every archive member has been validated.
        destination.mkdir(parents=False, exist_ok=False)
        hashes = {}
        for path, data in records:
            target = destination / str(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            hashes[str(path)] = hashlib.sha256(data).hexdigest()
    return {
        "tag": tag,
        "sha": sha,
        "source": str(destination.resolve()),
        "files_sha256": hashes,
        "export": "git archive; no source edits",
    }


def verify_export(record: dict[str, Any]) -> None:
    source = Path(record["source"])
    expected = record["files_sha256"]
    actual = {
        p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file()
    }
    if actual != expected or any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("FROZEN_EXPORT_CHANGED")


def save_record(path: Path, record: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
