"""Process separation, provenance, checksum, and summary contracts for the runner."""

from __future__ import annotations

import copy
import csv
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, replace
from functools import lru_cache
from pathlib import Path

import pytest
from benchmarks.campaign import (
    CampaignBundle,
    balanced_active_slots,
    deterministic_event_id,
    load_campaign,
)
from benchmarks.collectors import DatabaseSnapshot, EventObservation, ObservedEnvironment
from benchmarks.database_contract import (
    DatabaseDigests,
    StructuralSchemaIdentity,
    artifact_business_rows,
    compare_database_content,
    official_carrier_rows,
)
from benchmarks.run_campaign import (
    CampaignExecutionError,
    GitProvenance,
    ManagedProcess,
    _execute,
    _expected_warmup_events,
    _git_provenance,
    _locust_command,
    _project_release,
    _run_phase,
    _run_preparation,
    _validate_prepare_command,
    _verify_initial_state,
    _verify_measurement,
    _verify_warmup,
    _write_checksums,
    _write_summary,
)

FIXTURE = Path("benchmarks/fixtures/smoke-campaign.json")


@lru_cache(maxsize=1)
def _bundle() -> CampaignBundle:
    return load_campaign(FIXTURE)


def test_runner_builds_physically_distinct_warmup_and_measurement_processes() -> None:
    bundle = _bundle()
    load = bundle.manifest.loads[0]
    warmup = _locust_command(
        container_id="loadgen-id",
        locustfile=Path("/work/benchmarks/locustfile.py"),
        base_url="http://app:8000",
        load=load,
        prefix=Path("/tmp/warmup/locust"),
        phase="warmup",
    )
    measurement = _locust_command(
        container_id="loadgen-id",
        locustfile=Path("/work/benchmarks/locustfile.py"),
        base_url="http://app:8000",
        load=load,
        prefix=Path("/tmp/measurement/locust"),
        phase="measurement",
    )

    assert warmup != measurement
    assert "BENCHMARK_PHASE=warmup" in warmup
    assert "BENCHMARK_PHASE=measurement" in measurement
    assert "/tmp/warmup/locust" in warmup
    assert "/tmp/measurement/locust" in measurement
    assert warmup[warmup.index("--users") + 1] == "4"
    assert measurement[measurement.index("--spawn-rate") + 1] == "2.0"


def test_run_phase_physically_separates_warmup_from_measurement_csvs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    load = bundle.manifest.loads[0]
    repetition = tmp_path / "repeat.partial"
    repetition.mkdir()
    observed = ObservedEnvironment(
        {},
        {"app": "app-id", "postgres": "postgres-id", "loadgen": "loadgen-id"},
        "fulfillflow",
        "fulfillflow_benchmark",
    )

    class FakeProcess:
        def __init__(self, command: list[str]) -> None:
            self.command = command

        def wait(self, _timeout: float) -> int:
            return 0

        def ensure_stopped(self) -> None:
            return None

    class FakeSampler:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

    def copy_phase(command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        assert command[:2] == ["docker", "cp"]
        source = command[2]
        destination = Path(command[3])
        destination.mkdir(parents=True, exist_ok=True)
        phase = "warmup" if "/warmup/." in source else "measurement"
        (destination / "locust_stats_history.csv").write_text(
            f"timestamp,name\n1,{phase}-request\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("benchmarks.run_campaign.ManagedProcess", FakeProcess)
    monkeypatch.setattr("benchmarks.run_campaign.ResourceSampler", FakeSampler)
    monkeypatch.setattr("benchmarks.run_campaign._wait_for_container_file", lambda *_a: None)
    monkeypatch.setattr("benchmarks.run_campaign.run_capture", copy_phase)

    _run_phase(
        bundle,
        load,
        "warmup",
        "http://app:8000",
        observed,
        object(),  # type: ignore[arg-type]
        "/runtime/campaign.json",
        repetition,
    )
    _run_phase(
        bundle,
        load,
        "measurement",
        "http://app:8000",
        observed,
        object(),  # type: ignore[arg-type]
        "/runtime/campaign.json",
        repetition,
    )

    warmup_csv = repetition / "warmup" / "locust_stats_history.csv"
    measurement_csv = repetition / "locust_stats_history.csv"
    assert "warmup-request" in warmup_csv.read_text(encoding="utf-8")
    assert "warmup-request" not in measurement_csv.read_text(encoding="utf-8")
    assert "measurement-request" in measurement_csv.read_text(encoding="utf-8")
    _write_checksums(repetition)
    checksums = (repetition / "checksums.sha256").read_text(encoding="ascii")
    assert "  warmup/locust_stats_history.csv" in checksums
    assert "  locust_stats_history.csv" in checksums


def test_managed_process_timeout_terminates_process_group() -> None:
    process = ManagedProcess([sys.executable, "-c", "import time; time.sleep(30)"])

    with pytest.raises(CampaignExecutionError, match="timeout"):
        process.wait(0.01)

    process.ensure_stopped()
    assert process.process.poll() is not None


def test_managed_process_start_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_start(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic command path that must not be rendered")

    monkeypatch.setattr(subprocess, "Popen", fail_to_start)

    with pytest.raises(CampaignExecutionError, match="external process could not start") as error:
        ManagedProcess(["synthetic-sensitive-argument"])

    assert "sensitive" not in str(error.value)


def test_preparation_interruption_always_runs_conclusive_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptedProcess:
        stopped = False

        def wait(self, _timeout: float) -> int:
            raise KeyboardInterrupt

        def ensure_stopped(self) -> None:
            self.stopped = True

    process = InterruptedProcess()
    monkeypatch.setattr("benchmarks.run_campaign.ManagedProcess", lambda _command: process)

    with pytest.raises(KeyboardInterrupt):
        _run_preparation(["safe-prepare"], 1)

    assert process.stopped is True


def test_official_execution_refuses_dirty_tracked_source_before_creating_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    dirty = GitProvenance(bundle.manifest.git_sha, "release/v1.0.0", False, True)
    monkeypatch.setattr("benchmarks.run_campaign._git_provenance", lambda _path: dirty)
    official = bundle.manifest.model_copy(update={"official": True, "repetitions": 5})
    dirty_bundle = bundle.__class__(
        official,
        bundle.warmup,
        bundle.measurement,
        bundle.timeline,
        bundle.dataset_path,
        bundle.dataset_sha256,
        bundle.dataset_document,
    )
    results = tmp_path / "results"

    with pytest.raises(CampaignExecutionError, match="dirty"):
        _execute(dirty_bundle, FIXTURE, "http://app:8000", results, ["safe-prepare"])

    assert not results.exists()


def test_preparation_argv_rejects_sensitive_values() -> None:
    with pytest.raises(CampaignExecutionError, match="may not contain"):
        _validate_prepare_command(
            ["python", "seed.py", "postgresql+psycopg://user:password@db/database"]
        )


def test_checksums_cover_every_final_artifact_except_themselves(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text("{}\n", encoding="utf-8")
    nested = tmp_path / "warmup"
    nested.mkdir()
    (nested / "diagnostic.csv").write_text("value\n", encoding="utf-8")

    _write_checksums(tmp_path)

    lines = (tmp_path / "checksums.sha256").read_text(encoding="ascii").splitlines()
    assert len(lines) == 2
    assert any(line.endswith("  metadata.json") for line in lines)
    assert any(line.endswith("  warmup/diagnostic.csv") for line in lines)
    assert not any(line.endswith("  checksums.sha256") for line in lines)


def test_official_summary_uses_median_and_preserves_five_repetitions(tmp_path: Path) -> None:
    bundle = _bundle()
    official = bundle.manifest.model_copy(update={"official": True, "repetitions": 5})
    official_bundle = bundle.__class__(
        official,
        bundle.warmup,
        bundle.measurement,
        bundle.timeline,
        bundle.dataset_path,
        bundle.dataset_sha256,
        bundle.dataset_document,
    )
    repetitions: list[Path] = []
    for repetition, throughput in enumerate((10, 40, 30, 20, 50), start=1):
        directory = tmp_path / f"synthetic-4-users-r{repetition:02d}"
        directory.mkdir()
        repetitions.append(directory)
        _write_locust_aggregate(directory / "locust_stats.csv", throughput)

    _write_summary(tmp_path, official_bundle, repetitions)

    with (tmp_path / "summary.csv").open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert float(row["throughput"]) == 30
    assert float(row["error_rate"]) == pytest.approx(0.1)
    assert float(row["p50_ms"]) == 50
    assert float(row["p95_ms"]) == 95
    assert len(repetitions) == 5


def test_runner_derives_release_from_project_metadata() -> None:
    assert _project_release(Path.cwd()) == "v1.0.0"


def test_git_provenance_has_a_finite_noninteractive_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked_git(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[0] == "git"
        assert kwargs["timeout"] == 10.0
        assert kwargs["shell"] is False
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GCM_INTERACTIVE"] == "Never"
        raise subprocess.TimeoutExpired(command, 10.0)

    monkeypatch.setattr(subprocess, "run", blocked_git)

    with pytest.raises(CampaignExecutionError, match="frozen timeout") as error:
        _git_provenance(Path.cwd())

    assert "command" not in str(error.value).casefold()


def test_verify_initial_state_accepts_complete_authenticated_database() -> None:
    bundle = _bundle()
    database = _LogicalDatabase(bundle)

    snapshot, identity = _verify_initial_state(bundle, database)  # type: ignore[arg-type]

    assert snapshot.label == "initial"
    assert identity.dataset.global_sha256
    assert identity.carriers.global_sha256
    assert identity.structural_schema is not None
    assert identity.structural_schema.expected == bundle.manifest.database.schema_sha256
    assert identity.structural_schema.observed == bundle.manifest.database.schema_sha256
    assert identity.structural_schema.matches is True
    assert (
        identity.structural_schema.contract_version
        == bundle.manifest.database.structural_contract_version
    )
    assert identity.structural_schema.expected_alembic_heads == tuple(
        bundle.manifest.database.alembic_heads
    )
    assert identity.structural_schema.observed_alembic_heads == tuple(
        bundle.manifest.database.alembic_heads
    )
    assert identity.structural_schema.alembic_matches is True
    assert asdict(identity)["structural_schema"] == {
        "contract_version": bundle.manifest.database.structural_contract_version,
        "expected": bundle.manifest.database.schema_sha256,
        "observed": bundle.manifest.database.schema_sha256,
        "matches": True,
        "expected_alembic_heads": tuple(bundle.manifest.database.alembic_heads),
        "observed_alembic_heads": tuple(bundle.manifest.database.alembic_heads),
        "alembic_matches": True,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "recipient",
        "relation",
        "raw-body",
        "parsed-payload",
        "payload-hash",
        "timeline-shipment",
    ],
)
def test_verify_initial_state_rejects_content_drift_with_equal_counts(
    mutation: str,
) -> None:
    bundle = _bundle()
    database = _LogicalDatabase(bundle)
    if mutation == "recipient":
        database.rows["orders"][0]["recipient_city"] = "Different Synthetic City"
    elif mutation == "relation":
        database.rows["shipments"][0]["order_id"] = database.rows["shipments"][1]["order_id"]
    elif mutation == "raw-body":
        database.rows["carrier_event_inbox"][0]["raw_body"] = b'{"synthetic":"different"}'
    elif mutation == "parsed-payload":
        database.rows["carrier_event_inbox"][0]["parsed_payload"] = {"synthetic": "different"}
    elif mutation == "payload-hash":
        inbox = database.rows["carrier_event_inbox"][0]
        inbox["payload_sha256"] = "f" * 64
    else:
        timeline_id = str(bundle.timeline[0].shipment_id)
        row = next(item for item in database.rows["shipments"] if str(item["id"]) == timeline_id)
        row["tracking_code"] = "TIMELINE-CONTENT-DRIFT"

    with pytest.raises(CampaignExecutionError, match="sanitized content verification"):
        _verify_initial_state(bundle, database)  # type: ignore[arg-type]


def test_zero_exit_preparation_does_not_bypass_full_database_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    database = _LogicalDatabase(bundle)
    database.rows["orders"][0]["external_reference"] = "ORDER-CONTENT-DRIFT"

    class SuccessfulPreparation:
        def wait(self, _timeout: float) -> int:
            return 0

        def ensure_stopped(self) -> None:
            return None

    monkeypatch.setattr(
        "benchmarks.run_campaign.ManagedProcess", lambda _command: SuccessfulPreparation()
    )
    _run_preparation(["synthetic-prepare"], 1)

    with pytest.raises(CampaignExecutionError, match="sanitized content verification"):
        _verify_initial_state(bundle, database)  # type: ignore[arg-type]


def test_verify_initial_state_rejects_structural_digest_drift_before_content() -> None:
    bundle = _bundle()
    database = _LogicalDatabase(bundle)
    database.structure = StructuralSchemaIdentity(
        contract_version=bundle.manifest.database.structural_contract_version,
        sha256="f" * 64,
    )

    with pytest.raises(CampaignExecutionError, match="schema failed structural verification"):
        _verify_initial_state(bundle, database)  # type: ignore[arg-type]

    assert database.snapshot_calls == 0
    assert bundle.manifest.database.schema_sha256 != database.structure.sha256


def test_nonofficial_execution_does_not_bypass_alembic_or_structural_verification() -> None:
    bundle = _bundle()
    assert bundle.manifest.official is False
    database = _LogicalDatabase(bundle)
    database.heads = ("divergent_head",)

    with pytest.raises(CampaignExecutionError, match="Alembic head"):
        _verify_initial_state(bundle, database)  # type: ignore[arg-type]

    assert database.snapshot_calls == 0


def test_verify_warmup_accepts_complete_logical_identity() -> None:
    bundle = _bundle()
    database, initial, after = _warmup_database(bundle)

    identity = _verify_warmup(
        bundle,
        bundle.manifest.loads[0],
        database,  # type: ignore[arg-type]
        initial,
        after,
    )

    assert identity.dataset.global_sha256


@pytest.mark.parametrize(
    "mutation",
    [
        "occurred-at",
        "payload",
        "payload-hash",
        "normalized-status",
        "notification-link",
        "shipment-ordering",
        "measurement-cohort",
        "initial-event",
    ],
)
def test_verify_warmup_rejects_logical_drift_with_preserved_cardinalities(
    mutation: str,
) -> None:
    bundle = _bundle()
    database, initial, after = _warmup_database(bundle)
    event_id = sorted(database.observations)[0]
    observation = database.observations[event_id]
    if mutation == "occurred-at":
        database.observations[event_id] = replace(
            observation, occurred_at="2026-09-01T00:00:00.999999Z"
        )
    elif mutation == "payload":
        database.observations[event_id] = replace(
            observation, parsed_payload={"synthetic": "different"}
        )
    elif mutation == "payload-hash":
        database.observations[event_id] = replace(observation, payload_sha256="0" * 64)
    elif mutation == "normalized-status":
        database.observations[event_id] = replace(observation, canonical_status="EXCEPTION")
    elif mutation == "notification-link":
        database.observations[event_id] = replace(observation, matching_notification_count=0)
    elif mutation == "shipment-ordering":
        shipment_id = observation.shipment_id
        state = database.states[shipment_id]
        database.states[shipment_id] = (
            state[0],
            "2026-09-01T00:00:00.999999Z",
            state[2],
            state[3],
        )
    elif mutation == "measurement-cohort":
        shipment_id = str(bundle.measurement[0].shipment_id)
        state = database.states[shipment_id]
        database.states[shipment_id] = ("EXCEPTION", state[1], state[2], state[3])
    else:
        database.rows["tracking_events"][0]["description"] = "altered initial event"

    with pytest.raises(CampaignExecutionError):
        _verify_warmup(
            bundle,
            bundle.manifest.loads[0],
            database,  # type: ignore[arg-type]
            initial,
            after,
        )


def test_verify_measurement_accepts_only_consistent_applied_deltas(tmp_path: Path) -> None:
    bundle = _bundle()
    before = _snapshot(bundle, "pre_measurement")
    after = _snapshot(bundle, "post_measurement", applied_delta=7)
    _write_operational_http(tmp_path, {"APPLIED": 7})

    _verify_measurement(tmp_path, before, after)


@pytest.mark.parametrize(
    ("results", "database_delta"),
    [({"APPLIED": 7}, 6), ({"APPLIED": 7, "NO_STATE_CHANGE": 1}, 7)],
)
def test_verify_measurement_rejects_cardinally_consistent_but_invalid_outcomes(
    tmp_path: Path,
    results: dict[str, int],
    database_delta: int,
) -> None:
    bundle = _bundle()
    before = _snapshot(bundle, "pre_measurement")
    after = _snapshot(bundle, "post_measurement", applied_delta=database_delta)
    _write_operational_http(tmp_path, results)

    with pytest.raises(CampaignExecutionError):
        _verify_measurement(tmp_path, before, after)


def _write_locust_aggregate(path: Path, throughput: int) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "Name",
                "Request Count",
                "Failure Count",
                "Requests/s",
                "Median Response Time",
                "95%",
            ),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(
            {
                "Name": "Aggregated",
                "Request Count": 100,
                "Failure Count": 10,
                "Requests/s": throughput,
                "Median Response Time": 50,
                "95%": 95,
            }
        )


class _LogicalDatabase:
    def __init__(self, bundle: object) -> None:
        self.bundle = bundle
        document = bundle.dataset_document  # type: ignore[attr-defined]
        self.rows = copy.deepcopy(artifact_business_rows(document))
        self.carriers = copy.deepcopy(official_carrier_rows())
        shipments = document["tables"]["shipments"]  # type: ignore[index]
        self.states = {
            str(row["id"]): (
                str(row["status"]),
                str(row["status_occurred_at"]),
                ""
                if row["status_event_received_at"] is None
                else str(row["status_event_received_at"]),
                ""
                if row["status_external_event_id"] is None
                else str(row["status_external_event_id"]),
            )
            for row in shipments
        }
        tracking = document["tables"]["tracking_events"]  # type: ignore[index]
        self.event_counts = dict(Counter(str(row["shipment_id"]) for row in tracking))
        self.observations: dict[str, EventObservation] = {}
        manifest = bundle.manifest  # type: ignore[attr-defined]
        self.heads = tuple(manifest.database.alembic_heads)
        self.structure = StructuralSchemaIdentity(
            manifest.database.structural_contract_version,
            manifest.database.schema_sha256,
        )
        self.snapshot_calls = 0

    def alembic_heads(self) -> tuple[str, ...]:
        return self.heads

    def structural_schema_identity(self) -> StructuralSchemaIdentity:
        return self.structure

    def snapshot(self, label: str) -> DatabaseSnapshot:
        self.snapshot_calls += 1
        return _snapshot(self.bundle, label)

    def frozen_content_identity(
        self,
        document: object,
        *,
        allow_additional_rows: bool = False,
        ignored_columns_by_row: object = None,
    ) -> DatabaseDigests:
        return compare_database_content(
            artifact_business_rows(document),  # type: ignore[arg-type]
            self.rows,
            allow_additional_rows=allow_additional_rows,
            ignored_columns_by_row=ignored_columns_by_row,  # type: ignore[arg-type]
        )

    def official_carrier_identity(self) -> DatabaseDigests:
        return compare_database_content(
            {"carriers": official_carrier_rows()}, {"carriers": self.carriers}
        )

    def cohort_states(self, shipment_ids: list[str]) -> dict[str, tuple[str, str, str, str]]:
        return {item: self.states[item] for item in shipment_ids if item in self.states}

    def shipment_event_counts(self, shipment_ids: list[str]) -> dict[str, int]:
        return {item: self.event_counts[item] for item in shipment_ids if item in self.event_counts}

    def event_observations(self, event_ids: list[str]) -> dict[str, EventObservation]:
        return {item: self.observations[item] for item in event_ids if item in self.observations}


def _snapshot(
    bundle: object,
    label: str,
    *,
    applied_delta: int = 0,
) -> DatabaseSnapshot:
    document = bundle.dataset_document  # type: ignore[attr-defined]
    counts = dict(document["metadata"]["counts"])
    for table in ("carrier_event_inbox", "tracking_events", "notifications"):
        counts[table] += applied_delta
    inbox_statuses = dict(
        Counter(str(row["status"]) for row in document["tables"]["carrier_event_inbox"])
    )
    tracking_results = dict(
        Counter(str(row["application_result"]) for row in document["tables"]["tracking_events"])
    )
    inbox_statuses["PROCESSED"] = inbox_statuses.get("PROCESSED", 0) + applied_delta
    tracking_results["APPLIED"] = tracking_results.get("APPLIED", 0) + applied_delta
    return DatabaseSnapshot(label, counts, inbox_statuses, tracking_results)


def _warmup_database(
    bundle: object,
) -> tuple[_LogicalDatabase, DatabaseSnapshot, DatabaseSnapshot]:
    database = _LogicalDatabase(bundle)
    load = bundle.manifest.loads[0]  # type: ignore[attr-defined]
    active = balanced_active_slots(bundle.warmup, load.users)  # type: ignore[attr-defined]
    expected = _expected_warmup_events(bundle, load, active)  # type: ignore[arg-type]
    for index, (event_id, item) in enumerate(sorted(expected.items()), start=1):
        received_at = f"2026-09-01T00:01:{index:02d}.000000Z"
        database.observations[event_id] = EventObservation(
            external_event_id=event_id,
            inbox_id=f"inbox-{index}",
            inbox_status="PROCESSED",
            inbox_carrier_id=item.carrier_id,
            inbox_received_at=received_at,
            inbox_processed_at=received_at,
            payload_sha256=item.payload_sha256,
            raw_body_sha256=item.payload_sha256,
            parsed_payload=item.parsed_payload,
            tracking_event_id=f"event-{index}",
            event_received_at=received_at,
            shipment_id=item.shipment_id,
            event_carrier_id=item.carrier_id,
            external_status=item.external_status,
            canonical_status=item.canonical_status,
            occurred_at=item.occurred_at,
            application_result="APPLIED",
            previous_shipment_status=item.previous_status,
            resulting_shipment_status=item.resulting_status,
            notification_id=f"notification-{index}",
            notification_count=1,
            matching_notification_count=1,
        )
    quota = bundle.manifest.warmup_quota_per_shipment  # type: ignore[attr-defined]
    rows_by_id = {str(row["id"]): row for row in database.rows["shipments"]}
    for slot in active:
        last_event_id = deterministic_event_id(
            bundle.manifest.profile,  # type: ignore[attr-defined]
            load.name,
            "warmup",
            slot.slot_id,
            quota,
        )
        observation = database.observations[last_event_id]
        database.states[str(slot.shipment_id)] = (
            slot.initial_status,
            observation.occurred_at,
            observation.event_received_at,
            last_event_id,
        )
        database.event_counts[str(slot.shipment_id)] += quota
        row = rows_by_id[str(slot.shipment_id)]
        row["status_occurred_at"] = observation.occurred_at
        row["status_event_received_at"] = observation.event_received_at
        row["status_external_event_id"] = last_event_id
        row["updated_at"] = observation.event_received_at
    initial = _snapshot(bundle, "initial")
    after = _snapshot(bundle, "pre_measurement", applied_delta=load.users * quota)
    return database, initial, after


def _write_operational_http(path: Path, results: dict[str, int]) -> None:
    with (path / "operational_results.http.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("result", "count"))
        writer.writerows(sorted(results.items()))
