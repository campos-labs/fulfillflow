"""Offline derived loadgens with exhaustive benchmark-file and dependency comparison."""

import json
import shutil
from pathlib import Path
from typing import Any

from benchmarks.collectors import run_capture
from benchmarks.controls_v10 import _sha256
from benchmarks.operational_errors import write_report

FILES = ("campaign.py", "locustfile.py", "warmup_sensitivity.py")


def inventory(identifier: str) -> dict[str, Any]:
    script = (
        "import hashlib,json,importlib.metadata; from pathlib import Path; "
        "root=Path('/work/benchmarks'); "
        "print(json.dumps({'files':{str(p.relative_to(root)):"
        "hashlib.sha256(p.read_bytes()).hexdigest() "
        "for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}, "
        "'packages':sorted((d.metadata['Name'],d.version) "
        "for d in importlib.metadata.distributions())}))"
    )
    value = json.loads(
        run_capture(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                identifier,
                "-B",
                "-c",
                script,
            ],
            60,
        ).stdout
    )
    if not isinstance(value, dict):
        raise ValueError("loadgen inventory must be an object")
    return value


def derive(
    root: Path, destination: Path, version: str, parent: str, revision: str
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    before = inventory(parent)
    tag = f"fulfillflow-loadgen:sensitivity-win9445-01-{version}"
    parent_tag = f"fulfillflow-loadgen:sensitivity-parent-win9445-01-{version}"
    for reference in (tag, parent_tag):
        existing = run_capture(["docker", "image", "ls", "--quiet", reference], 30).stdout.strip()
        if existing:
            raise ValueError("derived image destination already exists; no replacement")
    run_capture(["docker", "tag", parent, parent_tag], 30)
    for name in FILES:
        shutil.copyfile(root / "benchmarks" / name, destination / name)
    (destination / "Dockerfile").write_text(
        f"FROM {parent_tag}\n"
        f'LABEL org.opencontainers.image.revision="{revision}"\n'
        f'LABEL org.fulfillflow.protocol="warmup-policy-sensitivity-v1"\n'
        f'LABEL org.fulfillflow.parent="{parent}"\n'
        f"COPY {' '.join(FILES)} /work/benchmarks/\n",
        encoding="utf-8",
    )
    completed = run_capture(
        [
            "docker",
            "build",
            "--pull=false",
            "--network=none",
            "--tag",
            tag,
            str(destination),
        ],
        180,
    )
    (destination / "build.txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    identifier = run_capture(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], 30
    ).stdout.strip()
    after = inventory(identifier)
    changed = sorted(
        key
        for key in before["files"].keys() | after["files"].keys()
        if before["files"].get(key) != after["files"].get(key)
    )
    if changed != sorted(FILES) or before["packages"] != after["packages"]:
        raise ValueError("derived loadgen differs beyond the authorized files")
    if any(after["files"][name] != _sha256(destination / name) for name in FILES):
        raise ValueError("derived loadgen source differs from the sealed build context")
    report = {
        "parent": parent,
        "image": identifier,
        "tag": tag,
        "source_revision": revision,
        "runner_lock_sha256": _sha256(root / "uv.lock"),
        "before": before,
        "after": after,
        "changed_files": changed,
        "dependencies_unchanged": True,
        "load_executed": False,
    }
    write_report(destination / "audit.json", report)
    return report
