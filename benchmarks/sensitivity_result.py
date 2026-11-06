"""Final warm-up evidence; incomplete quotas never become valid benchmark repetitions."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from benchmarks.campaign import CampaignBundle, LoadLevel
from benchmarks.collectors import (
    DatabaseProbe,
    DatabaseSnapshot,
    ObservedEnvironment,
    validate_resource_samples,
    write_database_counts,
)
from benchmarks.collectors_v11 import SplitDatabaseProbe, aggregate_resources
from benchmarks.operational_errors import write_report
from benchmarks.phase_diagnostics import warmup_quota_evidence
from benchmarks.warmup_sensitivity import PROTOCOL


class FinalProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    admission_seconds: Literal[60, 120]
    registered_users: StrictInt
    in_flight: StrictInt
    failure_code: Literal["quota_incomplete", "drain_timeout", "request_or_runtime_failure"] | None
    warmup_complete: bool
    applied_by_user: list[StrictInt]


def validate_progress(directory: Path, seconds: int, exit_code: int) -> FinalProgress:
    """Require exact per-user counters and the controlled reason exported by this image."""
    try:
        progress = FinalProgress.model_validate_json(
            (directory / "warmup-progress.json").read_text()
        )
    except ValidationError:
        raise ValueError("warm-up final progress schema is invalid") from None
    if (
        progress.admission_seconds != seconds
        or progress.registered_users != 12
        or progress.in_flight != 0
        or len(progress.applied_by_user) != 12
        or any(not 0 <= count <= 430 for count in progress.applied_by_user)
    ):
        raise ValueError("warm-up final progress violates admission/user/drain contracts")
    complete = all(count == 430 for count in progress.applied_by_user)
    if (
        complete != progress.warmup_complete
        or progress.failure_code != (None if complete else "quota_incomplete")
        or exit_code != (0 if complete else 2)
    ):
        raise ValueError("warm-up exit and recorded cause do not describe a pure quota outcome")
    quota = warmup_quota_evidence(
        directory,
        5160,
        final_statistics="locust_stats.csv" if exit_code == 0 else "locust_final_stats.csv",
    )
    if (
        quota.get("available") is not True
        or quota.get("applied") != sum(progress.applied_by_user)
        or quota.get("final_requests") != sum(progress.applied_by_user)
        or quota.get("final_http_failures") != 0
    ):
        raise ValueError("per-user progress and final HTTP evidence diverge")
    return progress


def execute_warmup_observation(
    bundle: CampaignBundle,
    load: LoadLevel,
    base_url: str,
    observed: ObservedEnvironment,
    database: DatabaseProbe,
    runtime_manifest: str,
    directory: Path,
    initial: DatabaseSnapshot,
    metadata: dict[str, object],
) -> int:
    # Import here because the shared runner dispatches this explicit diagnostic mode.
    from benchmarks.run_campaign import CampaignExecutionError, _run_phase, _verify_warmup

    metadata = {
        **metadata,
        "mode": PROTOCOL,
        "protocol_expected": bundle.manifest.model_dump(mode="json"),
        "matrix_eligible": False,
        "measurement_executed": False,
        "valid": False,
        "warmup_valid": False,
        "observation_integrity_verified": False,
        "container_ids": observed.container_ids,
        "environment_checks": observed.checks,
    }
    phase_dir = directory / "warmup"
    exit_code = 0
    try:
        try:
            phase = _run_phase(
                bundle, load, "warmup", base_url, observed, database, runtime_manifest, directory
            )
            metadata["warmup"] = asdict(phase) | {
                "started_at": phase.started_at.isoformat(),
                "finished_at": phase.finished_at.isoformat(),
            }
        except CampaignExecutionError:
            report = json.loads((phase_dir / "phase-error.json").read_text())
            if (
                report.get("stage") != "process_exit"
                or report.get("process_returncode") != 2
                or report.get("collector") is not None
                or report.get("secondary_errors") != []
                or "export_error" in report
            ):
                raise
            exit_code = 2
        exported = phase_dir / "failed-runtime" if exit_code else phase_dir
        for name in (
            "locust_stats_history.csv",
            "locust_failures.csv",
            "locust_exceptions.csv",
            "response_codes.csv",
            "operational_results.http.csv",
            "locust_final_stats.csv" if exit_code else "locust_stats.csv",
            "warmup-progress.json",
        ):
            if not (exported / name).is_file():
                raise CampaignExecutionError("sensitivity export is incomplete")
        progress = validate_progress(exported, bundle.manifest.warmup_seconds, exit_code)
        validate_resource_samples(phase_dir / "resources.csv", observed.container_ids)
        if isinstance(database, SplitDatabaseProbe) and exit_code:
            aggregate_resources(
                phase_dir / "resources.csv",
                phase_dir / "resources.application.csv",
                observed.container_ids,
            )
        after = database.snapshot("post_warmup_diagnostic")
        write_database_counts(directory / "database_counts.csv", [initial, after])
        identity = _verify_warmup(
            bundle,
            load,
            database,
            initial,
            after,
            applied_by_user=tuple(progress.applied_by_user),
        )
        if isinstance(database, SplitDatabaseProbe):
            write_report(directory / "reconciliation.json", database.reconcile())
        else:
            write_report(
                directory / "reconciliation.json",
                {
                    "applied": sum(progress.applied_by_user),
                    "verified": "per-user events, inbox, notification and database effects",
                },
            )
        metadata.update(
            warmup_valid=progress.warmup_complete,
            process_returncode=exit_code,
            observation_integrity_verified=True,
            recorded_failure_code=progress.failure_code,
            cause_basis="recorded_loadgen_reason_code",
            applied_by_user=progress.applied_by_user,
            logical_database_identity=asdict(identity),
            review_required=True,
        )
        return exit_code
    finally:
        write_report(directory / "metadata.json", metadata)
