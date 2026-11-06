"""Reuse sensitivity parents; only explicit full-protocol selection is added."""

import shutil
from pathlib import Path
from typing import Any

from benchmarks.collectors import run_capture
from benchmarks.controls_v10 import _sha256
from benchmarks.operational_errors import write_report
from benchmarks.sensitivity_images import inventory

FILES = ("locustfile.py", "comparison_protocol.py")


def derive(root: Path, destination: Path, version: str, parent: str) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    tag = f"fulfillflow-loadgen:comparison120-win9445-01-{version}"
    if run_capture(["docker", "image", "ls", "--quiet", tag], 30).stdout.strip():
        raise ValueError("comparison image tag exists; no replacement")
    before = inventory(parent)
    if before["files"]["campaign.py"] != _sha256(root / "benchmarks/campaign.py"):
        raise ValueError("parent campaign contract differs from reviewed host contract")
    parent_tag = f"fulfillflow-loadgen:comparison120-parent-win9445-01-{version}"
    if run_capture(["docker", "image", "ls", "--quiet", parent_tag], 30).stdout.strip():
        raise ValueError("comparison parent reference already exists")
    run_capture(["docker", "tag", parent, parent_tag], 30)
    for name in FILES:
        shutil.copyfile(root / "benchmarks" / name, destination / name)
    (destination / "Dockerfile").write_text(
        f"FROM {parent_tag}\n"
        f'LABEL org.fulfillflow.parent="{parent}"\n'
        'LABEL org.fulfillflow.protocol="symmetric-warmup120-comparison-v1"\n'
        f"COPY {' '.join(FILES)} /work/benchmarks/\n",
        encoding="utf-8",
    )
    result = run_capture(
        ["docker", "build", "--pull=false", "--network=none", "--tag", tag, str(destination)], 180
    )
    (destination / "build.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    identifier = run_capture(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], 30
    ).stdout.strip()
    after = inventory(identifier)
    changed = sorted(
        k
        for k in before["files"].keys() | after["files"].keys()
        if before["files"].get(k) != after["files"].get(k)
    )
    if changed != sorted(FILES) or before["packages"] != after["packages"]:
        raise ValueError("comparison loadgen differs outside authorized protocol files")
    if any(after["files"][name] != _sha256(destination / name) for name in FILES):
        raise ValueError("comparison image source identity differs")
    report = {
        "parent": parent,
        "image": identifier,
        "tag": tag,
        "before": before,
        "after": after,
        "changed_files": changed,
        "dependencies_unchanged": True,
        "runner_lock_sha256": _sha256(root / "uv.lock"),
        "load_executed": False,
        "source_identity": "precommit reviewed file hashes; no application revision claim",
    }
    write_report(destination / "audit.json", report)
    return report
