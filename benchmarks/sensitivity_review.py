"""Explicit post-attempt review, never invoked by the manual load launcher."""

import csv
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks import paired_controls
from benchmarks.controls_v10 import ControlError, _sha256, _write_checksums
from benchmarks.operational_errors import write_report
from benchmarks.sensitivity_result import validate_progress

if TYPE_CHECKING:
    from benchmarks.sensitivity_controls import Attempt


def review_attempt(attempt: "Attempt") -> dict[str, Any]:
    from benchmarks.sensitivity_controls import PACKAGE, REVIEW, read, verify_package

    verify_package()
    paired_controls.verify_checksums(attempt.attempt)
    result = read(attempt.attempt / "result.json")
    metadata = read(attempt.attempt / "run/mixed-12-users-r01.partial/metadata.json")
    if (
        result.get("complete") is not True
        or result.get("package_sha256") != _sha256(PACKAGE / "checksums.sha256")
        or "diagnostic_or_shutdown_error" in result
        or metadata.get("observation_integrity_verified") is not True
        or metadata.get("measurement_executed") is not False
        or metadata.get("matrix_eligible") is not False
        or metadata.get("valid") is not False
    ):
        raise ControlError("preparation, collection, export or integrity failure blocks review")
    code = result.get("exit_code")
    if type(code) is not int or code not in {0, 2}:
        raise ControlError("unexpected process exit code")
    phase = attempt.attempt / "run/mixed-12-users-r01.partial/warmup"
    exported = phase / "failed-runtime" if code else phase
    progress = validate_progress(exported, attempt.seconds, code)
    if metadata.get("applied_by_user") != progress.applied_by_user:
        raise ControlError("verified database quota differs from loadgen counters")
    import json

    containers = json.loads((attempt.attempt / "container-final.json").read_text())
    expected_ids = set(metadata["container_ids"].values())
    actual = {item["id"] for item in containers if item["id"] in expected_ids}
    if actual != expected_ids or any(
        item["state"].get("OOMKilled") is not False
        or item["restart_count"] != 0
        or (item["id"] in expected_ids and item["state"].get("Running") is not True)
        for item in containers
    ):
        raise ControlError("restart, OOM or missing live container requires investigation")
    statistics = exported / ("locust_stats.csv" if code == 0 else "locust_final_stats.csv")
    with statistics.open(encoding="utf-8", newline="") as stream:
        total = [row for row in csv.DictReader(stream) if row["Name"] == "Aggregated"]
    if len(total) != 1:
        raise ControlError("aggregate statistics unavailable")
    report = {
        "attempt": attempt.name,
        "checksums_sha256": _sha256(attempt.attempt / "checksums.sha256"),
        "package_sha256": _sha256(PACKAGE / "checksums.sha256"),
        "warmup_valid": progress.warmup_complete,
        "recorded_failure_code": progress.failure_code,
        "applied_by_user": progress.applied_by_user,
        "applied": sum(progress.applied_by_user),
        "missing": 5160 - sum(progress.applied_by_user),
        "statistics": {
            key: total[0][key]
            for key in (
                "Request Count",
                "Failure Count",
                "Requests/s",
                "Average Response Time",
                "95%",
            )
        },
        "release_next": attempt.number < 8,
        "next_attempt": attempt.number + 1 if attempt.number < 8 else None,
        "matrix_eligible": False,
        "interpretation": "exploratory policy observation; no exclusive cause or stability claim",
    }
    destination: Path = REVIEW / f"{attempt.number:02d}"
    destination.mkdir(parents=True, exist_ok=False)
    write_report(destination / "review.json", report)
    _write_checksums(destination)
    return report
