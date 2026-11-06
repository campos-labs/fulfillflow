"""Independent warm-up contract and unchanged historical scheduling semantics."""

import json
import os
import shutil
import subprocess
import sys
import venv
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

os.environ["LOCUST_SKIP_MONKEY_PATCH"] = "1"

from benchmarks.campaign import CampaignManifest, load_campaign
from benchmarks.locustfile import CampaignRuntime, _warmup_scheduled_offset
from benchmarks.sensitivity_result import validate_progress
from benchmarks.warmup_sensitivity import PROTOCOL, SensitivityManifest


def manifest(seconds: int = 60) -> SensitivityManifest:
    document = json.loads(Path("benchmarks/fixtures/smoke-campaign.json").read_text())
    document.update(
        protocol=PROTOCOL,
        profile="mixed",
        warmup_seconds=seconds,
        stabilization_seconds=300,
        warmup_quota_per_shipment=430,
        repetitions=1,
        loads=[{"name": "mixed-12-users", "users": 12, "spawn_rate": 16}],
    )
    document["timeouts"]["warmup_process_seconds"] = seconds + 60
    return SensitivityManifest.model_validate(document)


@pytest.mark.parametrize("seconds", [60, 120])
def test_separate_contract_and_historical_rejection(seconds: int) -> None:
    candidate = manifest(seconds)
    with pytest.raises(ValidationError):
        CampaignManifest.model_validate(candidate.model_dump())
    historical = candidate.model_dump(exclude={"protocol"})
    if seconds == 60:
        assert CampaignManifest.model_validate(historical).warmup_seconds == 60
    else:
        with pytest.raises(ValidationError):
            CampaignManifest.model_validate(historical)


@pytest.mark.parametrize("seconds", [59, 90, 121, 300])
def test_no_unplanned_durations(seconds: int) -> None:
    with pytest.raises(ValidationError):
        manifest(seconds)


def test_schedule_preserves_every_historical_offset_and_scales_proportionally() -> None:
    offsets = []
    for sequence in range(430):
        for user in range(12):
            expected = (sequence * 12 + user + 1) * 60 / 5161
            assert _warmup_scheduled_offset(sequence, 430, 12, user) == expected
            assert _warmup_scheduled_offset(sequence, 430, 12, user, 120) == expected * 2
            offsets.append(expected)
    assert len(set(offsets)) == 5160
    assert 0 < min(offsets) < max(offsets) < 60


@pytest.mark.parametrize("seconds", [60, 120])
def test_admission_barrier_and_per_user_quota(
    monkeypatch: pytest.MonkeyPatch, seconds: int
) -> None:
    candidate = manifest(seconds)
    bundle = replace(
        load_campaign(Path("benchmarks/fixtures/smoke-campaign.json")), manifest=candidate
    )
    runtime = CampaignRuntime(bundle, candidate.loads[0], "warmup")
    assert not runtime.begin_request()
    for _ in range(12):
        runtime.register_user()
    monkeypatch.setattr("benchmarks.locustfile.time.monotonic", lambda: 100.0)
    runtime.begin_phase("warmup")
    assert runtime.begin_request()
    monkeypatch.setattr("benchmarks.locustfile.time.monotonic", lambda: 100.0 + seconds)
    assert not runtime.begin_request()
    runtime.stop_new_requests()
    runtime.finish_request()
    assert not runtime.begin_request()
    for index in range(12):
        for _ in range(430):
            runtime.record_warmup_applied(index)
    assert runtime.warmup_complete
    runtime._warmup_completed[0] -= 1
    runtime._warmup_completed[1] += 1
    assert sum(runtime._warmup_completed.values()) == 5160
    assert not runtime.warmup_complete


@pytest.mark.parametrize("seconds", [60, 120])
@pytest.mark.parametrize("count,code", [(430, 0), (291, 2)])
def test_final_progress_matches_per_user_and_http(tmp_path, seconds, count, code):
    write_progress(tmp_path, seconds, [count] * 12, code)
    assert validate_progress(tmp_path, seconds, code).warmup_complete is (code == 0)


def write_progress(path, seconds, counts, code):
    path.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 1,
        "admission_seconds": seconds,
        "registered_users": 12,
        "in_flight": 0,
        "failure_code": "quota_incomplete" if code else None,
        "warmup_complete": code == 0,
        "applied_by_user": counts,
    }
    (path / "warmup-progress.json").write_text(json.dumps(document))
    filename = "locust_final_stats.csv" if code else "locust_stats.csv"
    (path / filename).write_text(f"Name,Request Count,Failure Count\nAggregated,{sum(counts)},0\n")
    (path / "operational_results.http.csv").write_text(f"result,count\nAPPLIED,{sum(counts)}\n")


@pytest.mark.parametrize(
    "field,value",
    [
        ("in_flight", 1),
        ("registered_users", 11),
        ("admission_seconds", 60),
        ("failure_code", "drain_timeout"),
        ("failure_code", "password=secret"),
        ("applied_by_user", [430] * 11 + [431]),
        ("applied_by_user", [True] * 12),
        ("warmup_complete", True),
    ],
)
def test_progress_refuses_nonquota_failures_and_invalid_counters(tmp_path, field, value):
    write_progress(tmp_path, 120, [291] * 12, 2)
    path = tmp_path / "warmup-progress.json"
    document = json.loads(path.read_text())
    document[field] = value
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        validate_progress(tmp_path, 120, 2)


def test_historical_verifier_never_accepts_partial_quota():
    from benchmarks.run_campaign import CampaignExecutionError, _verify_warmup

    bundle = load_campaign(Path("benchmarks/fixtures/smoke-campaign.json"))
    with pytest.raises(CampaignExecutionError, match="explicit sensitivity"):
        _verify_warmup(bundle, bundle.manifest.loads[0], None, None, None, applied_by_user=(1,) * 4)


@pytest.mark.parametrize("corrupt", [None, "pointer", "payload", "count", "inactive"])
def test_partial_database_prefix_verifies_odd_even_zero_and_ownership(corrupt):
    from datetime import UTC, datetime, timedelta

    from benchmarks.campaign import balanced_active_slots, deterministic_event_id
    from benchmarks.run_campaign import CampaignExecutionError, _verify_warmup
    from tests.unit.test_benchmark_runner import _LogicalDatabase, _snapshot, _warmup_database

    bundle = replace(
        load_campaign(Path("benchmarks/fixtures/smoke-campaign.json")), manifest=manifest()
    )
    database, initial, _ = _warmup_database(bundle)
    for index, (key, event) in enumerate(database.observations.items()):
        instant = (datetime(2026, 9, 1, tzinfo=UTC) + timedelta(microseconds=index)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        database.observations[key] = replace(
            event, inbox_received_at=instant, inbox_processed_at=instant, event_received_at=instant
        )
    pristine = _LogicalDatabase(bundle)
    counts = (0, 1, 2, 17, 120, 219, 300, 301, 429, 430, 11, 22)
    active = balanced_active_slots(bundle.warmup, 12)
    retained = set()
    rows = {str(row["id"]): row for row in database.rows["shipments"]}
    original_rows = {str(row["id"]): row for row in pristine.rows["shipments"]}
    for slot, count in zip(active, counts, strict=True):
        identifier = str(slot.shipment_id)
        for sequence in range(1, count + 1):
            retained.add(
                deterministic_event_id("mixed", "mixed-12-users", "warmup", slot.slot_id, sequence)
            )
        database.event_counts[identifier] = pristine.event_counts[identifier] + count
        if count == 0:
            database.states[identifier] = pristine.states[identifier]
            rows[identifier].update(original_rows[identifier])
        else:
            key = deterministic_event_id("mixed", "mixed-12-users", "warmup", slot.slot_id, count)
            event = database.observations[key]
            database.states[identifier] = (
                event.canonical_status,
                event.occurred_at,
                event.event_received_at,
                key,
            )
            rows[identifier].update(
                status=event.canonical_status,
                status_occurred_at=event.occurred_at,
                status_event_received_at=event.event_received_at,
                status_external_event_id=key,
                updated_at=event.event_received_at,
            )
    database.observations = {
        key: value for key, value in database.observations.items() if key in retained
    }
    after = _snapshot(bundle, "diagnostic", applied_delta=sum(counts))
    if corrupt == "pointer":
        identifier = str(active[1].shipment_id)
        database.states[identifier] = (*database.states[identifier][:3], "incorrect")
    elif corrupt == "payload":
        key = next(iter(database.observations))
        database.observations[key] = replace(database.observations[key], raw_body_sha256="0" * 64)
    elif corrupt == "count":
        database.event_counts[str(active[1].shipment_id)] += 1
    elif corrupt == "inactive":
        database.states[str(bundle.measurement[0].shipment_id)] = ("bad", "bad", "bad", "bad")
    if corrupt:
        with pytest.raises(CampaignExecutionError):
            _verify_warmup(
                bundle, bundle.manifest.loads[0], database, initial, after, applied_by_user=counts
            )
    else:
        _verify_warmup(
            bundle, bundle.manifest.loads[0], database, initial, after, applied_by_user=counts
        )


def test_no_measurement_process_for_sensitivity(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from benchmarks import run_campaign as runner

    bundle = replace(
        load_campaign(Path("benchmarks/fixtures/smoke-campaign.json")), manifest=manifest(120)
    )
    monkeypatch.setattr(runner, "ManagedProcess", lambda *_: pytest.fail("must not start process"))
    with pytest.raises(runner.CampaignExecutionError, match="forbids measurement"):
        runner._run_phase(
            bundle,
            bundle.manifest.loads[0],
            "measurement",
            "unused",
            SimpleNamespace(container_ids={"loadgen": "id"}),
            None,
            "unused",
            tmp_path,
        )


def test_fixed_order_and_single_attempt_gate(tmp_path, monkeypatch):
    from benchmarks import sensitivity_controls as controls

    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    monkeypatch.setattr(controls, "REVIEW", tmp_path / "reviews")
    assert [(controls.Attempt(i).version, controls.Attempt(i).seconds) for i in range(1, 9)] == [
        ("v10", 60),
        ("v11", 120),
        ("v11", 60),
        ("v10", 120),
        ("v10", 120),
        ("v11", 60),
        ("v11", 120),
        ("v10", 60),
    ]
    assert len({controls.Attempt(i).project for i in range(1, 9)}) == 8
    controls.require_next(controls.Attempt(1))
    with pytest.raises(FileNotFoundError):
        controls.require_next(controls.Attempt(2))
    controls.Attempt(1).attempt.mkdir()
    with pytest.raises(controls.ControlError, match="no retry"):
        controls.require_next(controls.Attempt(1))


def test_coordinator_stops_after_one_failed_process(tmp_path, monkeypatch):
    from benchmarks import sensitivity_controls as controls

    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    monkeypatch.setattr(controls, "PACKAGE", tmp_path / "package")
    controls.PACKAGE.mkdir()
    (controls.PACKAGE / "checksums.sha256").write_text("sealed")
    monkeypatch.setattr(controls, "verify_package", lambda: {"runner": {}, "launcher": {}})
    monkeypatch.setattr(controls, "require_next", lambda *_: None)
    monkeypatch.setattr(controls, "require_absent", lambda *_: None)
    monkeypatch.setattr(controls, "preflight", lambda *_: None)
    monkeypatch.setattr(controls, "environment", lambda *_: {})
    calls = []
    attempt = controls.Attempt(1)

    def run(argv, *_):
        calls.append(argv)
        assert "--warmup-sensitivity" in argv
        destination = attempt.attempt / "run/mixed-12-users-r01.partial"
        destination.mkdir(parents=True)
        (destination / "metadata.json").write_text('{"observation_integrity_verified":false}')
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(controls, "_run_runner", run)
    assert controls.execute(attempt, {}) == 7
    assert len(calls) == 1
    assert not controls.Attempt(2).attempt.exists()
    assert controls.read(attempt.attempt / "result.json")["exit_code"] == 7


@pytest.mark.parametrize("seconds", [60, 120])
def test_supervision_uses_declared_deadline_and_explicit_protocol(tmp_path, monkeypatch, seconds):
    from types import SimpleNamespace

    from benchmarks import run_campaign as runner

    bundle = replace(
        load_campaign(Path("benchmarks/fixtures/smoke-campaign.json")), manifest=manifest(seconds)
    )
    commands, waits = [], []

    class Process:
        def __init__(self, argv):
            commands.append(argv)

        def wait(self, timeout, **_):
            waits.append(timeout)
            return 2

        def ensure_stopped(self):
            pass

    monkeypatch.setattr(runner, "ManagedProcess", Process)
    monkeypatch.setattr(
        runner,
        "ResourceSampler",
        lambda *_a, **_k: SimpleNamespace(start=lambda: None, stop=lambda: None, failure=None),
    )
    monkeypatch.setattr(runner, "_wait_for_container_file", lambda *_: None)
    monkeypatch.setattr(runner, "_terminate_container_phase", lambda *_: None)
    monkeypatch.setattr(
        runner, "run_capture", lambda *_: subprocess.CompletedProcess([], 0, "", "")
    )
    with pytest.raises(runner.CampaignExecutionError):
        runner._run_phase(
            bundle,
            bundle.manifest.loads[0],
            "warmup",
            "unused",
            SimpleNamespace(container_ids={"loadgen": "id"}),
            None,
            "manifest",
            tmp_path,
        )
    assert waits == [seconds + 60]
    assert f"BENCHMARK_WARMUP_SENSITIVITY={PROTOCOL}" in commands[0]
    report = json.loads((tmp_path / "warmup/phase-error.json").read_text())
    assert report["stage"] == "process_exit" and report["collector"] is None


@pytest.mark.parametrize("seconds", [60, 120])
def test_fixed_admission_then_drain_without_quota_catchup(monkeypatch, seconds):
    from types import SimpleNamespace

    from benchmarks import locustfile

    bundle = replace(
        load_campaign(Path("benchmarks/fixtures/smoke-campaign.json")), manifest=manifest(seconds)
    )
    runtime = CampaignRuntime(bundle, bundle.manifest.loads[0], "warmup")
    for _ in range(12):
        runtime.register_user()
    clock = [0.0]
    outcomes = []
    monkeypatch.setattr(locustfile.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        locustfile.gevent, "sleep", lambda value: clock.__setitem__(0, clock[0] + value)
    )
    monkeypatch.setattr(locustfile, "_write_runtime_file", lambda *_: None)
    monkeypatch.setattr(locustfile, "_quit_runner", lambda _env, **kwargs: outcomes.append(kwargs))
    locustfile._coordinate_campaign(SimpleNamespace(), runtime)
    assert seconds <= clock[0] < seconds + 0.02
    assert runtime.failure_code == "quota_incomplete"
    assert not runtime.begin_request()
    assert outcomes == [{"invalid": True}]


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows PowerShell launcher")
@pytest.mark.parametrize(
    "flags,argv",
    [
        (["-PlanOnly"], ["--plan-only"]),
        (["-PrepareOnly"], ["--prepare-only"]),
        (["-Attempt", "1"], ["--execute", "1"]),
        (["-Attempt", "8"], ["--execute", "8"]),
    ],
)
def test_real_powershell_single_attempt_and_exit(tmp_path, flags, argv):
    executable = Path(os.environ.get("CONTROL_TEST_PWSH", "missing"))
    if not executable.is_file():
        pytest.skip("set CONTROL_TEST_PWSH to the full verified executable")
    root = tmp_path / "repository with spaces"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts/Invoke-WarmupSensitivity.ps1"
    shutil.copyfile(Path("scripts/Invoke-WarmupSensitivity.ps1"), script)
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    (root / "benchmarks").mkdir()
    (root / "benchmarks/__init__.py").write_text("")
    (root / "benchmarks/sensitivity_controls.py").write_text(
        "import sys,json\njson.load(sys.stdin)\n"
        f"assert sys.argv[1:] == {argv!r}\n"
        "print('stage: simulated; diagnostics: preserved.json',flush=True)\nsys.exit(7)\n"
    )
    result = subprocess.run(
        [str(executable), "-NoProfile", "-File", str(script), *flags],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=""),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 7
    assert "preserved.json" in result.stdout
    assert "no automatic retry" in result.stderr
