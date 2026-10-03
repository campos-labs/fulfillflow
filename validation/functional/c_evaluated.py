"""Prepare an immutable 54-case manifest; execution requires a separate reviewed release."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from validation.functional.c_qualify import BASE, main
from validation.functional.c_runtime import save_record, verify_export
from validation.functional.c_sources import REFERENCES


def sequence() -> list[dict[str, Any]]:
    return [
        {"round": r, "action": a, "version": v, "destination": f"r{r:02d}-{a}-{v}"}
        for r in range(1, 4)
        for a in ("healthy", "duplicate", "conflict", "control", "kill", "unavailable")
        for v in REFERENCES
    ]


def tool_hashes() -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted([*BASE.glob("*.py"), *BASE.glob("*.ps1")])
    }


def protocol_hash() -> str:
    return hashlib.sha256((BASE / "README.md").read_bytes()).hexdigest()


def prepare(package: Path, destination: Path) -> None:
    if not package.resolve().is_relative_to(BASE) or not destination.resolve().is_relative_to(BASE):
        raise ValueError("OUTSIDE_SCOPE")
    if destination.exists():
        raise ValueError("DESTINATION_EXISTS")
    sources = {}
    for tag in REFERENCES:
        path = BASE / ".artifacts/c-runtimes-01" / tag / "source.json"
        record = json.loads(path.read_text())
        verify_export(record)
        sources[tag] = hashlib.sha256(path.read_bytes()).hexdigest()
    package.mkdir(exist_ok=False)
    (package / "protocol.md").write_bytes((BASE / "README.md").read_bytes())
    save_record(
        package / "manifest.json",
        {
            "released": False,
            "destination": str(destination.resolve()),
            "references": REFERENCES,
            "tools": tool_hashes(),
            "protocol_sha256": protocol_hash(),
            "sources": sources,
            "cases": sequence(),
            "policy": "PASS or qualified C4 INCONCLUSIVE; all other outcomes stop; no retry",
        },
    )


def execute(package: Path) -> None:
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    release = json.loads((package / "release.json").read_text())
    if release != {
        "approved": True,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }:
        raise ValueError("RELEASE_MISMATCH")
    if (
        manifest["tools"] != tool_hashes()
        or manifest["references"] != REFERENCES
        or manifest["cases"] != sequence()
        or manifest.get("protocol_sha256") != protocol_hash()
        or hashlib.sha256((package / "protocol.md").read_bytes()).hexdigest() != protocol_hash()
        or set(manifest["sources"]) != set(REFERENCES)
    ):
        raise ValueError("PACKAGE_IDENTITY_MISMATCH")
    for tag, digest in manifest["sources"].items():
        p = BASE / ".artifacts/c-runtimes-01" / tag / "source.json"
        if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise ValueError("SOURCE_IDENTITY_MISMATCH")
        verify_export(json.loads(p.read_text()))
    destination = Path(manifest["destination"])
    if not destination.resolve().is_relative_to(BASE):
        raise ValueError("OUTSIDE_SCOPE")
    destination.mkdir(exist_ok=False)
    summary: dict[str, Any] = {"cases": [], "complete": False}
    try:
        for item in sequence():
            if manifest["tools"] != tool_hashes() or manifest["protocol_sha256"] != protocol_hash():
                raise ValueError("PACKAGE_CHANGED_DURING_SEQUENCE")
            folder = destination / item["destination"]
            main(folder, (item["action"],), (item["version"],), evaluated=True)
            record = json.loads((folder / "summary.json").read_text())
            summary["cases"].append(
                {"case": item, "classification": record["cases"][0]["classification"]}
            )
        summary["complete"] = True
    except Exception as exc:
        summary["error_type"] = type(exc).__name__
        raise
    finally:
        save_record(destination / "summary.json", summary)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    execute(args.package.resolve())
