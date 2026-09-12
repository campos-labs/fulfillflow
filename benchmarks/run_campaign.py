"""Validate or execute a fail-closed two-process Locust campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from benchmarks.artifact import BENCHMARK_HASH_NAME, BENCHMARK_MANIFEST_NAME
from benchmarks.campaign import (
    CampaignBundle,
    CohortSlot,
    LoadLevel,
    SplitDatabases,
    balanced_active_slots,
    canonical_payload_bytes,
    carrier_payload,
    cycle_target_status,
    deterministic_event_id,
    load_campaign,
)
from benchmarks.collection_diagnostics import write_failure
from benchmarks.collectors import (
    DatabaseProbe,
    DatabaseSnapshot,
    DockerProbe,
    EnvironmentMismatchError,
    EventObservation,
    ExternalCommandError,
    ObservedEnvironment,
    ResourceSampler,
    run_capture,
    validate_resource_samples,
    write_database_counts,
)
from benchmarks.collectors_v11 import SplitDatabaseProbe, aggregate_resources
from benchmarks.comparison_protocol import (
    PROTOCOL as COMPARISON_PROTOCOL,
)
from benchmarks.comparison_protocol import (
    ComparisonManifest,
    load_comparison_campaign,
    verify_warmup_progress,
)
from benchmarks.database_contract import (
    DatabaseDigests,
    DatabaseIdentityError,
    StructuralSchemaIdentity,
)
from benchmarks.host_probe import HostProbe
from benchmarks.phase_diagnostics import phase_failure, warmup_quota_evidence
from benchmarks.semantic import normalize_frozen_payload, validate_semantic_document
from benchmarks.warmup_sensitivity import PROTOCOL, SensitivityManifest, load_sensitivity_campaign

ProcessPhase = Literal["warmup", "measurement"]
_SENSITIVE_ARGUMENT = re.compile(r"(?i)(://|password|secret|signature|raw[_-]?body|dsn)")
_GIT_TIMEOUT_SECONDS = 10.0


class CampaignExecutionError(RuntimeError):
    """Sanitized campaign failure that leaves an explicitly incomplete directory."""


class ProcessTimeoutError(CampaignExecutionError):
    """The original process deadline elapsed."""


class CollectionSupervisionError(ExternalCommandError):
    """Mandatory sampler failure notified the existing process supervisor."""


@dataclass(frozen=True, slots=True)
class GitProvenance:
    sha: str
    branch: str
    worktree_clean: bool
    staged_clean: bool


@dataclass(frozen=True, slots=True)
class PhaseExecution:
    phase: ProcessPhase
    started_at: datetime
    finished_at: datetime
    returncode: int


@dataclass(frozen=True, slots=True)
class LogicalDatabaseIdentity:
    dataset: DatabaseDigests
    carriers: DatabaseDigests
    structural_schema: StructuralSchemaVerification | None = None
    owner_schemas: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class StructuralSchemaVerification:
    contract_version: int
    expected: str
    observed: str
    matches: bool
    expected_alembic_heads: tuple[str, ...]
    observed_alembic_heads: tuple[str, ...]
    alembic_matches: bool


@dataclass(frozen=True, slots=True)
class ExpectedWarmupEvent:
    external_event_id: str
    shipment_id: str
    carrier_id: str
    payload_sha256: str
    parsed_payload: dict[str, object]
    external_status: str
    canonical_status: str
    occurred_at: str
    previous_status: str
    resulting_status: str


def main() -> int:
    """Validate by default; execution requires explicit confirmation and preparation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-campaign")
    parser.add_argument("--base-url")
    parser.add_argument("--results-directory", type=Path)
    parser.add_argument("--application-source", type=Path)
    parser.add_argument("--diagnostic-warmup-only", action="store_true")
    parser.add_argument("--warmup-sensitivity", action="store_true")
    parser.add_argument("--comparison-120", action="store_true")
    parser.add_argument(
        "--prepare-command-json",
        help="JSON argv array run before each repetition; secrets must be supplied via environment",
    )
    args = parser.parse_args()
    if args.validate_only and args.execute:
        parser.error("choose either --validate-only or --execute")
    if sum((args.warmup_sensitivity, args.diagnostic_warmup_only, args.comparison_120)) > 1:
        parser.error("choose one explicit protocol")
    loader = (
        load_comparison_campaign
        if args.comparison_120
        else load_sensitivity_campaign
        if args.warmup_sensitivity
        else load_campaign
    )
    bundle = loader(args.manifest.resolve())
    if not args.execute:
        _print_validation(bundle)
        return 0
    if args.confirm_campaign != bundle.manifest.name:
        parser.error("--confirm-campaign must literally match the manifest campaign name")
    if not args.base_url or args.results_directory is None:
        parser.error("--base-url and --results-directory are required for execution")
    if not args.prepare_command_json:
        parser.error("--prepare-command-json is required before every executable repetition")
    prepare_command = _parse_command(args.prepare_command_json)
    try:
        return _execute(
            bundle,
            args.manifest.resolve(),
            args.base_url,
            args.results_directory,
            prepare_command,
            application_source=args.application_source,
            diagnostic_warmup_only=args.diagnostic_warmup_only,
        )
    except KeyboardInterrupt:
        return 130
    except (CampaignExecutionError, EnvironmentMismatchError, ExternalCommandError) as exc:
        print(f"campaign refused: {exc}", file=sys.stderr)
        return 2


def _execute(
    bundle: CampaignBundle,
    manifest_path: Path,
    base_url: str,
    results_directory: Path,
    prepare_command: list[str],
    *,
    application_source: Path | None = None,
    diagnostic_warmup_only: bool = False,
) -> int:
    sensitivity = isinstance(bundle.manifest, SensitivityManifest)
    mode = (
        PROTOCOL
        if sensitivity
        else "diagnostic_warmup_only"
        if diagnostic_warmup_only
        else "campaign"
    )
    if diagnostic_warmup_only:
        _validate_warmup_diagnostic(bundle)
    runner_root = Path(__file__).resolve().parents[1]
    repository_root = application_source.resolve() if application_source else runner_root
    if application_source is not None:
        top = run_capture(
            ["git", "-C", str(repository_root), "rev-parse", "--show-toplevel"],
            _GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
        if Path(top).resolve() != repository_root:
            raise CampaignExecutionError("application source must be an exact checkout root")
    validate_semantic_document(bundle.dataset_document)
    provenance = _git_provenance(repository_root)
    release = _project_release(repository_root)
    if release != bundle.manifest.release or provenance.sha != bundle.manifest.git_sha:
        raise CampaignExecutionError("manifest release/SHA does not match the checked-out source")
    if bundle.manifest.official and not (provenance.worktree_clean and provenance.staged_clean):
        raise CampaignExecutionError(
            "official campaign refuses a dirty tracked worktree or staging"
        )
    host_runner = None
    if application_source is not None:
        if not (provenance.worktree_clean and provenance.staged_clean):
            raise CampaignExecutionError("separate application source must be clean")
        host_runner = runner_provenance(runner_root, require_clean=True)
    _validate_prepare_command(prepare_command)
    results_directory.mkdir(parents=True, exist_ok=False)
    campaign_marker = results_directory / ".incomplete.json"
    _write_json(
        campaign_marker,
        {
            "campaign": bundle.manifest.name,
            "complete": False,
            "started_at": _utc_now(),
            "mode": mode,
        },
    )
    completed_directories: list[Path] = []
    try:
        docker = DockerProbe(bundle, repository_root)
        host = HostProbe(
            bundle.manifest.host,
            official=bundle.manifest.official,
            timeout_seconds=bundle.manifest.timeouts.command_seconds,
        )
        try:
            host_identity = host.identity()
        except EnvironmentMismatchError as exc:
            _write_json(
                results_directory / "metadata.json", {"valid": False, "host_identity": exc.report}
            )
            raise
        _write_json(
            results_directory / "metadata.json",
            {
                "host_identity": host_identity,
                "host_runner": host_runner,
                "application_git": asdict(provenance),
            },
        )
        for load in bundle.manifest.loads:
            for repetition in range(1, bundle.manifest.repetitions + 1):
                final_directory = results_directory / f"{load.name}-r{repetition:02d}"
                partial_directory = results_directory / f"{load.name}-r{repetition:02d}.partial"
                partial_directory.mkdir(exist_ok=False)
                _write_json(
                    partial_directory / ".incomplete.json",
                    {"complete": False, "load": load.name, "repetition": repetition},
                )
                _run_preparation(prepare_command, bundle.manifest.timeouts.preparation_seconds)
                try:
                    observed = docker.observe()
                except EnvironmentMismatchError as exc:
                    _write_json(partial_directory / "environment-mismatch.json", exc.report)
                    raise
                database = DatabaseProbe(
                    observed.container_ids["postgres"],
                    observed.postgres_user,
                    observed.postgres_database,
                    timeout_seconds=bundle.manifest.timeouts.command_seconds,
                )
                if bundle.manifest.release == "v1.1.0":
                    database = SplitDatabaseProbe(
                        observed.container_ids["postgres"],
                        timeout_seconds=bundle.manifest.timeouts.command_seconds,
                    )
                initial, initial_identity = _verify_initial_state(bundle, database)
                runtime_manifest = _install_runtime_manifest(
                    bundle,
                    manifest_path,
                    observed.container_ids["loadgen"],
                    partial_directory,
                )
                stabilization = _stabilize(bundle.manifest.stabilization_seconds)
                preflight_metadata: dict[str, object] = {
                    "valid": False,
                    "mode": mode,
                    "official": bundle.manifest.official,
                    "host_identity": host_identity,
                    "stabilization": stabilization,
                    "host_runner": host_runner,
                }
                try:
                    host_state = host.dynamic(observed.container_ids)
                except EnvironmentMismatchError as exc:
                    preflight_metadata["host_state"] = exc.report
                    _write_json(partial_directory / "metadata.json", preflight_metadata)
                    raise
                preflight_metadata["host_state"] = host_state
                _write_json(partial_directory / "metadata.json", preflight_metadata)
                if sensitivity:
                    from benchmarks.sensitivity_result import execute_warmup_observation

                    result = execute_warmup_observation(
                        bundle,
                        load,
                        base_url,
                        observed,
                        database,
                        runtime_manifest,
                        partial_directory,
                        initial,
                        preflight_metadata,
                    )
                    _write_checksums(partial_directory)
                    _write_checksums(results_directory)
                    return result
                warmup = _run_phase(
                    bundle,
                    load,
                    "warmup",
                    base_url,
                    observed,
                    database,
                    runtime_manifest,
                    partial_directory,
                )
                after_warmup = database.snapshot("pre_measurement")
                if isinstance(bundle.manifest, ComparisonManifest):
                    verify_warmup_progress(partial_directory / "warmup", load.users)
                pre_measurement_identity = _verify_warmup(
                    bundle, load, database, initial, after_warmup
                )
                if diagnostic_warmup_only:
                    _finish_warmup_diagnostic(
                        partial_directory,
                        bundle,
                        {
                            **preflight_metadata,
                            "campaign": bundle.manifest.name,
                            "git": asdict(provenance),
                            "manifest_sha256": _file_sha256(manifest_path),
                            "dataset_sha256": bundle.dataset_sha256,
                            "uv_lock_sha256": _file_sha256(repository_root / "uv.lock"),
                            "environment_checks": observed.checks,
                            "container_ids": observed.container_ids,
                            "protocol_expected": bundle.manifest.model_dump(mode="json"),
                            "logical_database_identity": {
                                "initial": asdict(initial_identity),
                                "post_warmup_seeded_rows": asdict(pre_measurement_identity),
                            },
                            "warmup": _phase_metadata(warmup),
                        },
                        database,
                        initial,
                        after_warmup,
                    )
                    partial_directory.rename(results_directory / "warmup-diagnostic")
                    campaign_marker.unlink()
                    _write_checksums(results_directory)
                    return 0
                measurement = _run_phase(
                    bundle,
                    load,
                    "measurement",
                    base_url,
                    observed,
                    database,
                    runtime_manifest,
                    partial_directory,
                )
                final = database.snapshot("post_measurement")
                if isinstance(database, SplitDatabaseProbe):
                    _write_json(partial_directory / "reconciliation.json", database.reconcile())
                _verify_measurement(partial_directory, after_warmup, final)
                write_database_counts(
                    partial_directory / "database_counts.csv", [initial, after_warmup, final]
                )
                _merge_operational_results(partial_directory, after_warmup, final)
                _write_json(
                    partial_directory / "metadata.json",
                    _metadata(
                        bundle,
                        manifest_path,
                        repository_root,
                        load,
                        repetition,
                        provenance,
                        observed,
                        initial,
                        after_warmup,
                        final,
                        initial_identity,
                        pre_measurement_identity,
                        warmup,
                        measurement,
                        stabilization,
                        host_identity,
                        host_state,
                        host_runner=host_runner,
                    ),
                )
                _require_repetition_artifacts(partial_directory)
                if isinstance(database, SplitDatabaseProbe):
                    for artifact in (
                        "reconciliation.json",
                        "resources.application.csv",
                        "warmup/resources.application.csv",
                    ):
                        if not (partial_directory / artifact).is_file():
                            raise CampaignExecutionError("v1.1 owner evidence is incomplete")
                (partial_directory / ".incomplete.json").unlink()
                _write_checksums(partial_directory)
                partial_directory.rename(final_directory)
                completed_directories.append(final_directory)
        if bundle.manifest.official:
            _write_summary(results_directory, bundle, completed_directories)
        campaign_marker.unlink()
        return 0
    except KeyboardInterrupt:
        _write_json(
            campaign_marker,
            {"campaign": bundle.manifest.name, "complete": False, "interrupted": True},
        )
        raise
    except BaseException:
        for partial in results_directory.glob("*.partial"):
            _write_checksums(partial)
        _write_checksums(results_directory)
        raise


def _run_preparation(command: list[str], timeout_seconds: float) -> None:
    process = ManagedProcess(command)
    try:
        returncode = process.wait(timeout_seconds)
    finally:
        process.ensure_stopped()
    if returncode != 0:
        raise CampaignExecutionError("database preparation command failed")


def _validate_warmup_diagnostic(bundle: CampaignBundle) -> None:
    manifest = bundle.manifest
    if (
        manifest.official
        or manifest.release != "v1.1.0"
        or manifest.repetitions != 1
        or manifest.profile != "mixed"
        or len(manifest.loads) != 1
        or manifest.loads[0].users != 12
        or manifest.warmup_quota_per_shipment != 430
        or manifest.stabilization_seconds != 300
        or manifest.warmup_seconds != 60
        or manifest.measurement_seconds != 300
    ):
        raise CampaignExecutionError(
            "warm-up diagnostic requires the frozen nonofficial v1.1/12 contract"
        )


def _finish_warmup_diagnostic(
    directory: Path,
    bundle: CampaignBundle,
    metadata: dict[str, object],
    database: DatabaseProbe,
    initial: DatabaseSnapshot,
    after_warmup: DatabaseSnapshot,
) -> None:
    if not isinstance(database, SplitDatabaseProbe):
        raise CampaignExecutionError("warm-up diagnostic requires both database owners")
    _write_json(directory / "reconciliation.json", database.reconcile())
    write_database_counts(directory / "database_counts.csv", [initial, after_warmup])
    for name in (
        "locust_stats.csv",
        "locust_stats_history.csv",
        "locust_failures.csv",
        "locust_exceptions.csv",
        "response_codes.csv",
        "operational_results.http.csv",
        "resources.csv",
        "resources.application.csv",
    ):
        if not (directory / "warmup" / name).is_file():
            raise CampaignExecutionError("warm-up diagnostic artifacts are incomplete")
    quota = warmup_quota_evidence(
        directory / "warmup",
        12 * bundle.manifest.warmup_quota_per_shipment,
        final_statistics="locust_stats.csv",
    )
    if (
        quota.get("available") is not True
        or quota.get("applied") != 5160
        or quota.get("final_http_failures") != 0
    ):
        raise CampaignExecutionError("warm-up final HTTP evidence does not confirm the fixed quota")
    metadata.update(
        mode="diagnostic_warmup_only",
        official=False,
        valid=False,
        diagnostic_complete=True,
        warmup_valid=True,
        matrix_eligible=False,
        measurement_executed=False,
        quota_evidence=quota,
    )
    _write_json(directory / "metadata.json", metadata)
    (directory / ".incomplete.json").unlink()
    _write_checksums(directory)


def _stabilize(
    seconds: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    started_at = utc_now()
    started = monotonic()
    deadline = started + seconds
    while (remaining := deadline - monotonic()) > 0:
        sleep(remaining)
    return {
        "expected_seconds": seconds,
        "started_at": started_at.isoformat(),
        "finished_at": utc_now().isoformat(),
        "observed_seconds": monotonic() - started,
    }


def _run_phase(
    bundle: CampaignBundle,
    load: LoadLevel,
    phase: ProcessPhase,
    base_url: str,
    observed: ObservedEnvironment,
    database: DatabaseProbe,
    runtime_manifest: str,
    partial_directory: Path,
) -> PhaseExecution:
    container_id = observed.container_ids["loadgen"]
    token = hashlib.sha256(f"{partial_directory.resolve()}:{phase}".encode()).hexdigest()[:20]
    container_directory = f"/tmp/fulfillflow-benchmark/{token}/{phase}"
    marker = f"{container_directory}/phase-started.marker"
    completed_marker = f"{container_directory}/phase-completed.marker"
    pid_file = f"{container_directory}/locust.pid"
    command = _locust_command(
        container_id=container_id,
        locustfile=Path("/work/benchmarks/locustfile.py"),
        base_url=base_url,
        load=load,
        prefix=Path(f"{container_directory}/locust"),
        phase=phase,
        runtime_manifest=runtime_manifest,
        marker=marker,
        completed_marker=completed_marker,
        pid_file=pid_file,
        response_file=f"{container_directory}/response_codes.csv",
        operational_file=f"{container_directory}/operational_results.http.csv",
    )
    if isinstance(bundle.manifest, SensitivityManifest):
        if phase != "warmup":
            raise CampaignExecutionError("sensitivity protocol forbids measurement")
        command[2:2] = ["--env", f"BENCHMARK_WARMUP_SENSITIVITY={PROTOCOL}"]
    elif isinstance(bundle.manifest, ComparisonManifest):
        command[2:2] = ["--env", f"BENCHMARK_COMPARISON_PROTOCOL={COMPARISON_PROTOCOL}"]
    process: ManagedProcess | None = None
    sampler: ResourceSampler | None = None
    destination = partial_directory / "warmup" if phase == "warmup" else partial_directory
    started_at: datetime | None = None
    primary: BaseException | None = None
    secondary: list[dict[str, object]] = []
    supervision_started = time.monotonic()
    returncode: int | None = None
    stage = "process_start"
    try:
        process = ManagedProcess(command)
        stage = "phase_start"
        _wait_for_container_file(
            container_id,
            marker,
            bundle.manifest.timeouts.phase_start_seconds,
            bundle.manifest.timeouts.command_seconds,
        )
        started_at = datetime.now(UTC)
        stage = "collector"
        sampler = ResourceSampler(
            destination / "resources.csv",
            observed.container_ids,
            database,
            bundle.manifest.collection_interval_seconds,
            command_timeout_seconds=bundle.manifest.timeouts.command_seconds,
            phase=phase,
        )
        sampler.start()
        timeout = (
            bundle.manifest.timeouts.warmup_process_seconds
            if phase == "warmup"
            else bundle.manifest.timeouts.measurement_process_seconds
        )
        stage = "process_wait"
        returncode = process.wait(timeout, sampler=sampler)
        if returncode != 0:
            stage = "process_exit"
            raise CampaignExecutionError(f"{phase} Locust process invalidated the repetition")
    except BaseException as exc:
        primary = exc
        if isinstance(exc, CollectionSupervisionError):
            stage = "collector"
        elif isinstance(exc, ProcessTimeoutError):
            stage = "process_timeout"
        try:
            _terminate_container_phase(
                container_id, pid_file, bundle.manifest.timeouts.command_seconds
            )
        except BaseException as cleanup_error:
            secondary.append(phase_failure(cleanup_error, "shutdown", supervision_started))
    finally:
        try:
            if process is not None:
                process.ensure_stopped()
        except BaseException as cleanup_error:
            if primary is None:
                primary = cleanup_error
                stage = "shutdown"
            else:
                secondary.append(phase_failure(cleanup_error, "shutdown", supervision_started))
        if sampler is not None:
            try:
                sampler.stop()
            except BaseException as sampler_error:
                if primary is None:
                    primary = sampler_error
                    stage = "collector"
                else:
                    secondary.append(phase_failure(sampler_error, "collector", supervision_started))
    if primary is None:
        try:
            stage = "completion"
            _wait_for_container_file(
                container_id,
                completed_marker,
                bundle.manifest.timeouts.command_seconds,
                bundle.manifest.timeouts.command_seconds,
            )
            stage = "export"
            destination.mkdir(exist_ok=True)
            run_capture(
                ["docker", "cp", f"{container_id}:{container_directory}/.", str(destination)],
                bundle.manifest.timeouts.command_seconds,
            )
            stage = "validation"
            _remove_runtime_markers(destination)
            _promote_final_statistics(destination)
            validate_resource_samples(destination / "resources.csv", observed.container_ids)
            if isinstance(database, SplitDatabaseProbe):
                aggregate_resources(
                    destination / "resources.csv",
                    destination / "resources.application.csv",
                    observed.container_ids,
                )
        except BaseException as exc:
            primary = exc
    if primary is not None:
        report = phase_failure(primary, stage, supervision_started)
        report.update(
            phase=phase,
            complete=False,
            process_returncode=returncode,
            secondary_errors=secondary,
            cause_basis="recorded_process_exit"
            if stage == "process_exit"
            else "runner_observation",
            locust_internal_reason=None,
        )
        if sampler is not None:
            report["collector"] = sampler.failure
        try:
            destination.mkdir(parents=True, exist_ok=True)
            diagnostic = destination / "failed-runtime"
            diagnostic.mkdir(exist_ok=False)
            run_capture(
                ["docker", "cp", f"{container_id}:{container_directory}/.", str(diagnostic)],
                bundle.manifest.timeouts.command_seconds,
            )
        except BaseException as export_error:
            report["export_error"] = phase_failure(export_error, "export", supervision_started)
        if phase == "warmup":
            report["quota_evidence"] = warmup_quota_evidence(
                destination / "failed-runtime",
                load.users * bundle.manifest.warmup_quota_per_shipment,
            )
        try:
            write_failure(destination / "phase-error.json", report)
        except OSError:
            primary.add_note("phase diagnostic could not be written; preserve owned containers")
        raise primary
    if started_at is None:
        raise CampaignExecutionError(f"{phase} did not publish its real start marker")
    if returncode != 0:
        raise CampaignExecutionError(f"{phase} Locust process invalidated the repetition")
    return PhaseExecution(phase, started_at, datetime.now(UTC), returncode)


def _locust_command(
    *,
    container_id: str = "loadgen",
    locustfile: Path,
    base_url: str,
    load: LoadLevel,
    prefix: Path,
    phase: ProcessPhase = "measurement",
    runtime_manifest: str = "campaign.json",
    marker: str = "phase-started.marker",
    completed_marker: str = "phase-completed.marker",
    pid_file: str = "locust.pid",
    response_file: str = "response_codes.csv",
    operational_file: str = "operational_results.http.csv",
) -> list[str]:
    environment = {
        "BENCHMARK_CAMPAIGN_MANIFEST": runtime_manifest,
        "BENCHMARK_LOAD_NAME": load.name,
        "BENCHMARK_PHASE": phase,
        "BENCHMARK_PHASE_MARKER": marker,
        "BENCHMARK_PHASE_COMPLETED_MARKER": completed_marker,
        "BENCHMARK_PID_FILE": pid_file,
        "BENCHMARK_RESPONSE_CODES_FILE": response_file,
        "BENCHMARK_OPERATIONAL_RESULTS_FILE": operational_file,
    }
    command = ["docker", "exec"]
    for key, value in environment.items():
        command.extend(("--env", f"{key}={value}"))
    command.extend(
        (
            container_id,
            "python",
            "-m",
            "locust",
            "--locustfile",
            locustfile.as_posix(),
            "--headless",
            "--host",
            base_url,
            "--users",
            str(load.users),
            "--spawn-rate",
            str(load.spawn_rate),
            "--csv",
            prefix.as_posix(),
            "--csv-full-history",
            "--only-summary",
        )
    )
    return command


def _promote_final_statistics(directory: Path) -> None:
    """Require a final drained snapshot instead of trusting the periodic export."""
    try:
        final = directory / "locust_final_stats.csv"
        with final.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        aggregate = [row for row in rows if row["Name"] == "Aggregated"]
        with (directory / "response_codes.csv").open(encoding="utf-8", newline="") as stream:
            responses = sum(int(row["count"]) for row in csv.DictReader(stream))
        if len(aggregate) != 1 or int(aggregate[0]["Request Count"]) != responses:
            raise ValueError
        if (
            sum(int(row["Request Count"]) for row in rows if row["Name"] != "Aggregated")
            != responses
        ):
            raise ValueError
        final.replace(directory / "locust_stats.csv")
    except (OSError, KeyError, ValueError, csv.Error) as exc:
        raise CampaignExecutionError("final Locust statistics are missing or inconsistent") from exc


class ManagedProcess:
    """Bounded cross-platform process group with conclusive teardown."""

    def __init__(self, command: list[str]) -> None:
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
            start_new_session = False
        else:
            flags = 0
            start_new_session = True
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                start_new_session=start_new_session,
            )
        except OSError as exc:
            raise CampaignExecutionError("external process could not start") from exc

    def wait(self, timeout_seconds: float, *, sampler: ResourceSampler | None = None) -> int:
        if sampler is not None:
            deadline = time.monotonic() + timeout_seconds
            while True:
                if sampler.failed.is_set():
                    raise CollectionSupervisionError("mandatory resource sampling failed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.terminate()
                    raise ProcessTimeoutError("external process exceeded its frozen timeout")
                try:
                    return self.process.wait(timeout=min(0.25, remaining))
                except subprocess.TimeoutExpired:
                    continue
        try:
            return self.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self.terminate()
            raise ProcessTimeoutError("external process exceeded its frozen timeout") from exc

    def terminate(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                kill_process_group = getattr(os, "killpg")  # noqa: B009
                kill_process_group(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            if sys.platform == "win32":
                self.process.kill()
            else:
                try:
                    kill_process_group = getattr(os, "killpg")  # noqa: B009
                    kill_process_group(
                        self.process.pid,
                        getattr(signal, "SIGKILL", signal.SIGTERM),
                    )
                except ProcessLookupError:
                    pass
            self.process.wait(timeout=5)

    def ensure_stopped(self) -> None:
        self.terminate()
        if self.process.poll() is None:
            raise CampaignExecutionError("external process could not be terminated conclusively")


def _install_runtime_manifest(
    bundle: CampaignBundle,
    manifest_path: Path,
    loadgen_container_id: str,
    partial_directory: Path,
) -> str:
    token = hashlib.sha256(str(partial_directory.resolve()).encode("utf-8")).hexdigest()[:20]
    target = f"/tmp/fulfillflow-benchmark/{token}/campaign"
    runtime_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime_manifest["cohorts"]["dataset_manifest"] = BENCHMARK_MANIFEST_NAME
    with tempfile.TemporaryDirectory(prefix="fulfillflow-benchmark-") as temporary:
        root = Path(temporary)
        _write_json(root / "campaign.json", runtime_manifest)
        shutil.copyfile(bundle.dataset_path, root / BENCHMARK_MANIFEST_NAME)
        shutil.copyfile(
            bundle.dataset_path.with_name(BENCHMARK_HASH_NAME), root / BENCHMARK_HASH_NAME
        )
        run_capture(
            [
                "docker",
                "exec",
                loadgen_container_id,
                "python",
                "-c",
                "from pathlib import Path; Path(" + repr(target) + ").mkdir(parents=True)",
            ],
            bundle.manifest.timeouts.command_seconds,
        )
        for source in sorted(root.iterdir()):
            run_capture(
                ["docker", "cp", str(source), f"{loadgen_container_id}:{target}/{source.name}"],
                bundle.manifest.timeouts.command_seconds,
            )
    return f"{target}/campaign.json"


def _verify_initial_state(
    bundle: CampaignBundle,
    database: DatabaseProbe,
) -> tuple[DatabaseSnapshot, LogicalDatabaseIdentity]:
    database_contract = bundle.manifest.database
    owner_schemas = None
    if isinstance(database_contract, SplitDatabases):
        if not isinstance(database, SplitDatabaseProbe):
            raise CampaignExecutionError("v1.1 requires both owner probes")
        owner_schemas = database.verify_schemas(database_contract)
        database.reconcile(initial=True)
        database_contract = database_contract.core
    expected_heads = tuple(sorted(database_contract.alembic_heads))
    observed_heads = tuple(sorted(database.alembic_heads()))
    if observed_heads != expected_heads:
        raise CampaignExecutionError("prepared database Alembic head failed verification")
    try:
        observed_structure = database.structural_schema_identity()
    except DatabaseIdentityError:
        raise CampaignExecutionError(
            "prepared database schema failed structural verification"
        ) from None
    expected_structure = StructuralSchemaIdentity(
        contract_version=database_contract.structural_contract_version,
        sha256=database_contract.schema_sha256,
    )
    if observed_structure != expected_structure:
        raise CampaignExecutionError("prepared database schema failed structural verification")
    structural_verification = StructuralSchemaVerification(
        contract_version=observed_structure.contract_version,
        expected=expected_structure.sha256,
        observed=observed_structure.sha256,
        matches=True,
        expected_alembic_heads=expected_heads,
        observed_alembic_heads=observed_heads,
        alembic_matches=True,
    )
    snapshot = database.snapshot("initial")
    metadata = bundle.dataset_document["metadata"]
    assert isinstance(metadata, dict)
    if snapshot.counts != metadata["counts"]:
        raise CampaignExecutionError("preparation did not restore the frozen dataset cardinalities")
    if snapshot.inbox_statuses != {"PROCESSED": 15_000}:
        raise CampaignExecutionError("preparation did not restore 15,000 PROCESSED inboxes")
    tables = bundle.dataset_document["tables"]
    assert isinstance(tables, dict)
    tracking_rows = tables["tracking_events"]
    assert isinstance(tracking_rows, list)
    expected_results = Counter(str(item["application_result"]) for item in tracking_rows)
    if snapshot.tracking_results != dict(expected_results):
        raise CampaignExecutionError("preparation did not restore tracking result cardinalities")
    slots = (*bundle.warmup, *bundle.measurement)
    observed = database.cohort_states([str(item.shipment_id) for item in slots])
    expected = {
        str(item.shipment_id): _initial_shipment_state(bundle, str(item.shipment_id))
        for item in slots
    }
    if observed != expected:
        raise CampaignExecutionError("preparation did not restore every mutable cohort state")
    try:
        dataset_identity = database.frozen_content_identity(bundle.dataset_document)
        carrier_identity = database.official_carrier_identity()
    except DatabaseIdentityError as exc:
        raise CampaignExecutionError(
            f"prepared database {exc.table} failed sanitized {exc.mismatch_class} verification"
        ) from None
    return snapshot, LogicalDatabaseIdentity(
        dataset_identity,
        carrier_identity,
        structural_schema=structural_verification,
        owner_schemas=owner_schemas,
    )


def _verify_warmup(
    bundle: CampaignBundle,
    load: LoadLevel,
    database: DatabaseProbe,
    initial: DatabaseSnapshot,
    after: DatabaseSnapshot,
    *,
    applied_by_user: tuple[int, ...] | None = None,
) -> LogicalDatabaseIdentity:
    if applied_by_user is not None and (
        not isinstance(bundle.manifest, SensitivityManifest)
        or len(applied_by_user) != load.users
        or any(type(count) is not int or not 0 <= count <= 430 for count in applied_by_user)
    ):
        raise CampaignExecutionError("partial quota requires the explicit sensitivity protocol")
    quotas = applied_by_user or (bundle.manifest.warmup_quota_per_shipment,) * load.users
    expected_delta = sum(quotas)
    if isinstance(database, SplitDatabaseProbe):
        database.reconcile()
        if (
            after.auxiliary_counts["core_receipts"] - initial.auxiliary_counts["core_receipts"]
            != expected_delta
        ):
            raise CampaignExecutionError("warm-up receipt delta differs from fixed quota")
    active = balanced_active_slots(bundle.warmup, load.users)
    quota = bundle.manifest.warmup_quota_per_shipment
    expected_events = _expected_warmup_events(bundle, load, active)
    if applied_by_user is not None:
        admitted_ids = {
            deterministic_event_id(bundle.manifest.profile, load.name, "warmup", slot.slot_id, seq)
            for slot, count in zip(active, quotas, strict=True)
            for seq in range(1, count + 1)
        }
        expected_events = {
            key: value for key, value in expected_events.items() if key in admitted_ids
        }
    observed_events = database.event_observations(sorted(expected_events))
    if set(observed_events) != set(expected_events):
        raise CampaignExecutionError("warm-up did not persist its exact deterministic event quota")
    for event_id, expected in expected_events.items():
        if not _warmup_observation_matches(observed_events[event_id], expected):
            raise CampaignExecutionError(
                "warm-up event logical identity does not match its exclusive slot"
            )
    if (
        len({item.inbox_id for item in observed_events.values()}) != len(observed_events)
        or len({item.tracking_event_id for item in observed_events.values()})
        != len(observed_events)
        or len({item.notification_id for item in observed_events.values()}) != len(observed_events)
    ):
        raise CampaignExecutionError("warm-up event persistence identities are not one-to-one")
    for table in ("carrier_event_inbox", "tracking_events", "notifications"):
        if after.counts[table] - initial.counts[table] != expected_delta:
            raise CampaignExecutionError(f"warm-up {table} delta does not match the fixed quota")
    if (
        after.counts["orders"] != initial.counts["orders"]
        or after.counts["shipments"] != initial.counts["shipments"]
    ):
        raise CampaignExecutionError("warm-up changed Order/Shipment cardinality")
    all_slots = (*bundle.warmup, *bundle.measurement)
    states = database.cohort_states([str(item.shipment_id) for item in all_slots])
    active_by_id = {str(item.shipment_id): item for item in active}
    counts_by_id = {
        str(slot.shipment_id): count for slot, count in zip(active, quotas, strict=True)
    }
    for slot in bundle.warmup:
        state = states.get(str(slot.shipment_id))
        active_slot = active_by_id.get(str(slot.shipment_id))
        quota = counts_by_id.get(str(slot.shipment_id), 0)
        if active_slot is None or quota == 0:
            expected_state = _initial_shipment_state(bundle, str(slot.shipment_id))
            if state != expected_state:
                raise CampaignExecutionError("warm-up touched an inactive warm-up Shipment")
            continue
        last_event_id = deterministic_event_id(
            bundle.manifest.profile,
            load.name,
            "warmup",
            active_slot.slot_id,
            quota,
        )
        last_occurred = bundle.manifest.warmup_occurred_at_base + timedelta(
            microseconds=quota * bundle.manifest.occurred_at_step_microseconds
        )
        expected_logical = (
            cycle_target_status(active_slot.initial_status, quota),
            last_occurred.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            last_event_id,
        )
        if state is None or (state[0], state[1], state[3]) != expected_logical:
            raise CampaignExecutionError(
                "warm-up Shipment did not return to its exact logical final state"
            )
        if state[2] != observed_events[last_event_id].event_received_at:
            raise CampaignExecutionError(
                "warm-up Shipment ordering pointer does not match its final event"
            )
    for slot in bundle.measurement:
        expected_state = _initial_shipment_state(bundle, str(slot.shipment_id))
        if states.get(str(slot.shipment_id)) != expected_state:
            raise CampaignExecutionError("warm-up touched the measurement cohort")
    seeded_counts = _seeded_event_counts(bundle)
    observed_counts = database.shipment_event_counts([str(item.shipment_id) for item in all_slots])
    expected_counts = {
        shipment_id: count + counts_by_id.get(shipment_id, 0)
        for shipment_id, count in seeded_counts.items()
        if shipment_id in {str(item.shipment_id) for item in all_slots}
    }
    if observed_counts != expected_counts:
        raise CampaignExecutionError(
            "warm-up changed event ownership outside its active exclusive cohort"
        )
    try:
        ignored_shipment_columns = {
            str(slot.shipment_id): frozenset(
                {
                    "status_occurred_at",
                    "status_event_received_at",
                    "status_external_event_id",
                    "updated_at",
                    *({"status"} if applied_by_user is not None else set()),
                }
            )
            for slot in active
        }
        dataset_identity = database.frozen_content_identity(
            bundle.dataset_document,
            allow_additional_rows=True,
            ignored_columns_by_row={"shipments": ignored_shipment_columns},
        )
        carrier_identity = database.official_carrier_identity()
    except DatabaseIdentityError as exc:
        raise CampaignExecutionError(
            f"post-warm-up database {exc.table} failed sanitized {exc.mismatch_class} verification"
        ) from None
    return LogicalDatabaseIdentity(dataset_identity, carrier_identity)


def _verify_measurement(
    directory: Path,
    before: DatabaseSnapshot,
    after: DatabaseSnapshot,
) -> None:
    http = _read_result_counts(directory / "operational_results.http.csv")
    applied = http.get("APPLIED", 0)
    values = {
        applied,
        after.counts["carrier_event_inbox"] - before.counts["carrier_event_inbox"],
        after.counts["tracking_events"] - before.counts["tracking_events"],
        after.counts["notifications"] - before.counts["notifications"],
        after.tracking_results.get("APPLIED", 0) - before.tracking_results.get("APPLIED", 0),
    }
    if len(values) != 1:
        raise CampaignExecutionError(
            "measurement HTTP/APPLIED and database persistence deltas are inconsistent"
        )
    if (
        after.auxiliary_counts
        and after.auxiliary_counts["core_receipts"] - before.auxiliary_counts["core_receipts"]
        != applied
    ):
        raise CampaignExecutionError("measurement receipts differ from HTTP effects")
    if any(key != "APPLIED" and value > 0 for key, value in http.items()):
        raise CampaignExecutionError("measurement contains a non-APPLIED webhook outcome")


def _merge_operational_results(
    directory: Path,
    before: DatabaseSnapshot,
    after: DatabaseSnapshot,
) -> None:
    http_path = directory / "operational_results.http.csv"
    http = _read_result_counts(http_path)
    rows: list[tuple[str, str, int]] = [("http", key, value) for key, value in sorted(http.items())]
    results = set(before.tracking_results) | set(after.tracking_results)
    rows.extend(
        (
            "database_tracking",
            result,
            after.tracking_results.get(result, 0) - before.tracking_results.get(result, 0),
        )
        for result in sorted(results)
    )
    rows.append(
        (
            "database_inbox",
            "REJECTED",
            after.inbox_statuses.get("REJECTED", 0) - before.inbox_statuses.get("REJECTED", 0),
        )
    )
    with (directory / "operational_results.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("source", "result", "count"))
        writer.writerows(rows)
    http_path.unlink()


def _metadata(
    bundle: CampaignBundle,
    manifest_path: Path,
    repository_root: Path,
    load: LoadLevel,
    repetition: int,
    provenance: GitProvenance,
    observed: ObservedEnvironment,
    initial: DatabaseSnapshot,
    after_warmup: DatabaseSnapshot,
    final: DatabaseSnapshot,
    initial_identity: LogicalDatabaseIdentity,
    pre_measurement_identity: LogicalDatabaseIdentity,
    warmup: PhaseExecution,
    measurement: PhaseExecution,
    stabilization: dict[str, object],
    host_identity: dict[str, dict[str, object]],
    host_state: dict[str, dict[str, object]],
    *,
    host_runner: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "campaign": bundle.manifest.name,
        "official": bundle.manifest.official,
        "valid": True,
        "release": bundle.manifest.release,
        "profile": bundle.manifest.profile,
        "load": load.model_dump(mode="json"),
        "repetition": repetition,
        "git": asdict(provenance),
        "host_runner": host_runner,
        "manifest_sha256": _file_sha256(manifest_path),
        "dataset_sha256": bundle.dataset_sha256,
        "uv_lock_sha256": _file_sha256(repository_root / "uv.lock"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "environment_checks": observed.checks,
        "host_identity": host_identity,
        "host_state": host_state,
        "stabilization": stabilization,
        "container_ids": observed.container_ids,
        "protocol_expected": bundle.manifest.model_dump(mode="json", exclude={"loads"}),
        "database_snapshots": [
            {"label": item.label, "metrics": item.metrics()}
            for item in (initial, after_warmup, final)
        ],
        "logical_database_identity": {
            "initial": asdict(initial_identity),
            "pre_measurement_seeded_rows": asdict(pre_measurement_identity),
        },
        "warmup": _phase_metadata(warmup),
        "measurement": _phase_metadata(measurement),
    }


def _phase_metadata(phase: PhaseExecution) -> dict[str, object]:
    return {
        "process": phase.phase,
        "started_at": phase.started_at.isoformat(),
        "finished_at": phase.finished_at.isoformat(),
        "exit_code": phase.returncode,
    }


def _write_summary(
    results_directory: Path,
    bundle: CampaignBundle,
    repetitions: list[Path],
) -> None:
    rows: list[tuple[str, float, float, float, float]] = []
    for load in bundle.manifest.loads:
        metrics = [
            _locust_aggregate(directory / "locust_stats.csv")
            for directory in repetitions
            if directory.name.startswith(f"{load.name}-")
        ]
        if len(metrics) != bundle.manifest.repetitions:
            raise CampaignExecutionError("official summary refuses missing or invalid repetitions")
        rows.append(
            (
                load.name,
                statistics.median(item["throughput"] for item in metrics),
                statistics.median(item["error_rate"] for item in metrics),
                statistics.median(item["p50"] for item in metrics),
                statistics.median(item["p95"] for item in metrics),
            )
        )
    with (results_directory / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("load", "throughput", "error_rate", "p50_ms", "p95_ms"))
        writer.writerows(rows)


def _locust_aggregate(path: Path) -> dict[str, float]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    try:
        aggregate = next(item for item in rows if item.get("Name") == "Aggregated")
        requests = float(aggregate["Request Count"])
        failures = float(aggregate["Failure Count"])
        return {
            "throughput": float(aggregate["Requests/s"]),
            "error_rate": 0 if requests == 0 else failures / requests,
            "p50": float(aggregate["Median Response Time"]),
            "p95": float(aggregate["95%"]),
        }
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise CampaignExecutionError("Locust aggregate CSV is missing required metrics") from exc


def _write_checksums(directory: Path) -> None:
    paths = sorted(
        path for path in directory.rglob("*") if path.is_file() and path.name != "checksums.sha256"
    )
    with (directory / "checksums.sha256").open("w", encoding="ascii", newline="\n") as stream:
        for path in paths:
            stream.write(f"{_file_sha256(path)}  {path.relative_to(directory).as_posix()}\n")


def _require_repetition_artifacts(directory: Path) -> None:
    required = {
        "locust_stats.csv",
        "locust_stats_history.csv",
        "locust_failures.csv",
        "locust_exceptions.csv",
        "response_codes.csv",
        "operational_results.csv",
        "resources.csv",
        "warmup/resources.csv",
        "database_counts.csv",
        "metadata.json",
    }
    missing = sorted(name for name in required if not (directory / name).is_file())
    if missing:
        raise CampaignExecutionError(
            f"repetition is missing required artifacts: {', '.join(missing)}"
        )


def _wait_for_container_file(
    container_id: str,
    path: str,
    timeout_seconds: float,
    command_timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    command = [
        "docker",
        "exec",
        container_id,
        "python",
        "-c",
        "from pathlib import Path; "
        "raise SystemExit(0 if Path(" + repr(path) + ").is_file() else 1)",
    ]
    while time.monotonic() < deadline:
        try:
            run_capture(command, command_timeout_seconds)
            return
        except ExternalCommandError:
            time.sleep(0.05)
    raise CampaignExecutionError("Locust phase marker did not appear before its frozen timeout")


def _terminate_container_phase(container_id: str, pid_file: str, timeout_seconds: float) -> None:
    script = (
        "from pathlib import Path; import os,signal; p=Path(" + repr(pid_file) + "); "
        "os.kill(int(p.read_text().strip()), signal.SIGTERM) if p.is_file() else None"
    )
    try:
        run_capture(["docker", "exec", container_id, "python", "-c", script], timeout_seconds)
    except ExternalCommandError:
        pass


def _remove_runtime_markers(directory: Path) -> None:
    for name in ("phase-started.marker", "phase-completed.marker", "locust.pid"):
        path = directory / name
        if path.exists():
            path.unlink()


def _initial_shipment_row(bundle: CampaignBundle, shipment_id: str) -> dict[str, object]:
    tables = bundle.dataset_document["tables"]
    assert isinstance(tables, dict)
    shipments = tables["shipments"]
    assert isinstance(shipments, list)
    try:
        return next(item for item in shipments if str(item["id"]) == shipment_id)
    except StopIteration as exc:
        raise CampaignExecutionError("cohort Shipment is absent from authenticated tables") from exc


def _initial_shipment_state(
    bundle: CampaignBundle,
    shipment_id: str,
) -> tuple[str, str, str, str]:
    row = _initial_shipment_row(bundle, shipment_id)
    received_at = row["status_event_received_at"]
    event_id = row["status_external_event_id"]
    return (
        str(row["status"]),
        str(row["status_occurred_at"]),
        "" if received_at is None else str(received_at),
        "" if event_id is None else str(event_id),
    )


def _expected_warmup_events(
    bundle: CampaignBundle,
    load: LoadLevel,
    active: tuple[CohortSlot, ...],
) -> dict[str, ExpectedWarmupEvent]:
    expected: dict[str, ExpectedWarmupEvent] = {}
    for slot in active:
        carrier_id = (
            "00000000-0000-4000-8000-000000000100"
            if slot.carrier_code == "carrier-alpha"
            else "00000000-0000-4000-8000-000000000101"
        )
        for sequence in range(1, bundle.manifest.warmup_quota_per_shipment + 1):
            event_id = deterministic_event_id(
                bundle.manifest.profile,
                load.name,
                "warmup",
                slot.slot_id,
                sequence,
            )
            target = cycle_target_status(slot.initial_status, sequence)
            previous = (
                slot.initial_status
                if sequence == 1
                else cycle_target_status(slot.initial_status, sequence - 1)
            )
            occurred_at = bundle.manifest.warmup_occurred_at_base + timedelta(
                microseconds=(sequence * bundle.manifest.occurred_at_step_microseconds)
            )
            payload = carrier_payload(slot, event_id, target, occurred_at)
            normalized = normalize_frozen_payload(
                "alpha" if slot.carrier_code == "carrier-alpha" else "beta",
                payload,
            )
            expected[event_id] = ExpectedWarmupEvent(
                external_event_id=event_id,
                shipment_id=str(slot.shipment_id),
                carrier_id=carrier_id,
                payload_sha256=hashlib.sha256(canonical_payload_bytes(payload)).hexdigest(),
                parsed_payload=payload,
                external_status=normalized.external_status,
                canonical_status=target,
                occurred_at=occurred_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                previous_status=previous,
                resulting_status=target,
            )
    return expected


def _warmup_observation_matches(
    observed: EventObservation,
    expected: ExpectedWarmupEvent,
) -> bool:
    return (
        observed.external_event_id == expected.external_event_id
        and observed.inbox_status == "PROCESSED"
        and observed.shipment_id == expected.shipment_id
        and observed.inbox_carrier_id == expected.carrier_id
        and observed.event_carrier_id == expected.carrier_id
        and observed.payload_sha256 == expected.payload_sha256
        and observed.raw_body_sha256 == expected.payload_sha256
        and observed.parsed_payload == expected.parsed_payload
        and observed.inbox_received_at == observed.event_received_at
        and bool(observed.inbox_processed_at)
        and observed.external_status == expected.external_status
        and observed.canonical_status == expected.canonical_status
        and observed.occurred_at == expected.occurred_at
        and observed.application_result == "APPLIED"
        and observed.previous_shipment_status == expected.previous_status
        and observed.resulting_shipment_status == expected.resulting_status
        and observed.notification_count == 1
        and observed.matching_notification_count == 1
    )


def _seeded_event_counts(bundle: CampaignBundle) -> dict[str, int]:
    tables = bundle.dataset_document["tables"]
    assert isinstance(tables, dict)
    rows = tables["tracking_events"]
    assert isinstance(rows, list)
    return dict(Counter(str(item["shipment_id"]) for item in rows))


def _read_result_counts(path: Path) -> dict[str, int]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    try:
        return {str(item["result"]): int(item["count"]) for item in rows}
    except (KeyError, TypeError, ValueError) as exc:
        raise CampaignExecutionError("operational result CSV is invalid") from exc


def runner_provenance(repository_root: Path, *, require_clean: bool = True) -> dict[str, object]:
    """Identify the host tool independently of the frozen measured application."""
    provenance = _git_provenance(repository_root)
    if require_clean and not (provenance.worktree_clean and provenance.staged_clean):
        raise CampaignExecutionError("reviewed host runner must have a clean tracked source")
    paths = [
        *sorted((repository_root / "benchmarks").glob("*.py")),
        *sorted((repository_root / "src").rglob("*.py")),
        repository_root / "uv.lock",
        repository_root / "pyproject.toml",
    ]
    return {
        "schema_version": 1,
        "git": asdict(provenance),
        "uv_lock_sha256": _file_sha256(repository_root / "uv.lock"),
        "components": {
            path.relative_to(repository_root).as_posix(): _file_sha256(path) for path in paths
        },
        "supervision_interval_seconds": 0.25,
        "collection_policy": "original-deltas-no-retry-failure-diagnostics-v1",
        "phase_diagnostics": "separate-process-collector-shutdown-export-v2",
        "diagnostic_mode": "explicit-warmup-only-not-matrix-eligible",
    }


def _git_provenance(repository_root: Path) -> GitProvenance:
    def git(*args: str) -> str:
        environment = os.environ.copy()
        environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT_SECONDS,
                env=environment,
                shell=False,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise CampaignExecutionError(
                "Git provenance could not be determined within its frozen timeout"
            ) from exc
        return completed.stdout.strip()

    return GitProvenance(
        sha=git("rev-parse", "HEAD"),
        branch=git("branch", "--show-current"),
        worktree_clean=not bool(git("diff", "--name-only")),
        staged_clean=not bool(git("diff", "--cached", "--name-only")),
    )


def _git_sha() -> str:
    """Compatibility helper for callers that need only the checked-out SHA."""
    return _git_provenance(Path.cwd()).sha


def _parse_command(value: str) -> list[str]:
    try:
        command = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit("--prepare-command-json must be valid JSON") from exc
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise SystemExit("--prepare-command-json must be a non-empty JSON string array")
    return command


def _validate_prepare_command(command: list[str]) -> None:
    if any(_SENSITIVE_ARGUMENT.search(item) for item in command):
        raise CampaignExecutionError(
            "preparation argv may not contain a DSN, password, secret, signature or raw body"
        )


def _project_release(repository_root: Path) -> str:
    document = tomllib.loads((repository_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = document["project"]["version"]
    if not isinstance(version, str) or not version:
        raise RuntimeError("project version is absent from pyproject.toml")
    return f"v{version}"


def _print_validation(bundle: CampaignBundle) -> None:
    manifest = bundle.manifest
    print(
        json.dumps(
            {
                "name": manifest.name,
                "official": manifest.official,
                "profile": manifest.profile,
                "dataset_sha256": bundle.dataset_sha256,
                "loads": [item.model_dump(mode="json") for item in manifest.loads],
                "repetitions": manifest.repetitions,
                "warmup_processes": 1,
                "measurement_processes": 0 if isinstance(manifest, SensitivityManifest) else 1,
                "warmup_seconds": manifest.warmup_seconds,
                "measurement_seconds": 0
                if isinstance(manifest, SensitivityManifest)
                else manifest.measurement_seconds,
                "warmup_quota_per_shipment": manifest.warmup_quota_per_shipment,
                "warmup_cohort": len(bundle.warmup),
                "measurement_cohort": len(bundle.measurement),
                "timeline_cohort": len(bundle.timeline),
            },
            indent=2,
            sort_keys=True,
        )
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
