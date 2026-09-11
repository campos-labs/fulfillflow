"""Sanitized phase failures, independent of mandatory collector diagnostics."""

from __future__ import annotations

import csv
from pathlib import Path

from benchmarks.collection_diagnostics import failure

MESSAGES = {
    "process_start": "Locust command could not start",
    "phase_start": "Locust start marker was not confirmed",
    "process_wait": "Locust process supervision failed",
    "process_timeout": "Locust process exceeded its frozen timeout",
    "process_exit": "Locust process exited with a nonzero code",
    "collector": "mandatory resource collection failed",
    "shutdown": "phase process shutdown failed",
    "completion": "Locust completion marker was not confirmed",
    "export": "phase artifact export failed",
    "validation": "exported phase artifacts failed validation",
}


def phase_failure(error: BaseException, stage: str, started: float) -> dict[str, object]:
    """Keep exception classes/codes, never arbitrary exception text or subprocess output."""
    report = failure(error, "snapshot", started)
    report.update(schema_version=2, stage=stage, message=MESSAGES[stage])
    return report


def warmup_quota_evidence(
    directory: Path, expected: int, *, final_statistics: str = "locust_final_stats.csv"
) -> dict[str, object]:
    """Derive quota evidence from final exported tallies, never Locust's internal reason."""
    report: dict[str, object] = {
        "basis": "derived_from_exported_artifacts",
        "locust_internal_reason": None,
        "expected_applied": expected,
        "available": False,
    }
    try:
        with (directory / "operational_results.http.csv").open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        with (directory / final_statistics).open(newline="", encoding="utf-8") as f:
            aggregates = [row for row in csv.DictReader(f) if row["Name"] == "Aggregated"]
        applied_rows = [row for row in rows if row["result"] == "APPLIED"]
        if len(aggregates) != 1 or len(applied_rows) != 1:
            return report
        applied = int(applied_rows[0]["count"])
        requests = int(aggregates[0]["Request Count"])
        failures = int(aggregates[0]["Failure Count"])
        if not 0 <= applied <= requests or not 0 <= failures <= requests:
            return report
        report.update(
            available=True,
            applied=applied,
            final_requests=requests,
            final_http_failures=failures,
            quota_incomplete=applied < expected,
            missing_applied=max(0, expected - applied),
        )
    except (OSError, KeyError, ValueError, csv.Error):
        pass  # Missing/invalid evidence is not a fabricated cause or a relaxed gate.
    return report
