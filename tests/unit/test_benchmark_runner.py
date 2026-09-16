"""Process separation, provenance, checksum, and summary contracts for the runner."""

from __future__ import annotations

import copy
import csv
import json
import os
import signal
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import pytest
from benchmarks.campaign import (
    CampaignBundle,
    balanced_active_slots,
    deterministic_event_id,
    load_campaign,
)
from benchmarks.collectors import (
    DatabaseSnapshot,
    EnvironmentMismatchError,
    EventObservation,
    ObservedEnvironment,
)
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
    LogicalDatabaseIdentity,
    ManagedProcess,
    PhaseExecution,
    _execute,
    _expected_warmup_events,
    _git_provenance,
    _locust_command,
    _project_release,
    _require_repetition_artifacts,
    _run_phase,
    _run_preparation,
    _stabilize,
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

        def wait(self, _timeout: float, **_kwargs: object) -> int:
            return 0

        def ensure_stopped(self) -> None:
            return None

    class FakeSampler:
        def __init__(self, path: Path, *_args: object, **_kwargs: object) -> None:
            self.path = path

        def start(self) -> None:
            _write_resources(self.path)

        def stop(self) -> None:
            return None

    def copy_phase(command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        assert command[:2] == ["docker", "cp"]
        source = command[2]
        destination = Path(command[3])
        destination.mkdir(parents=True, exist_ok=True)
        phase = "warmup" if "/warmup/." in source else "measurement"
        (destination / "locust_final_stats.csv").write_text(
            "Name,Request Count\nroute,1\nAggregated,1\n", encoding="utf-8"
        )
        (destination / "response_codes.csv").write_text(
            "status_code,problem_code,count\n200,,1\n", encoding="utf-8"
        )
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
    assert "  warmup/resources.csv" in checksums
    assert "  resources.csv" in checksums


def test_managed_process_timeout_terminates_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    class TimedOutProcess:
        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired("synthetic", timeout)

    terminated: list[bool] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: TimedOutProcess())
    monkeypatch.setattr(ManagedProcess, "terminate", lambda _self: terminated.append(True))
    process = ManagedProcess(["synthetic"])

    with pytest.raises(CampaignExecutionError, match="timeout"):
        process.wait(0.01)

    assert terminated == [True]


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_phase_supervision_refuses_failure_despite_healthy_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    bundle = _bundle()
    observed = ObservedEnvironment(
        {"loadgen.health": {"matches": True, "observed": "healthy"}},
        {"app": "app-id", "postgres": "postgres-id", "loadgen": "loadgen-id"},
        "fulfillflow",
        "fulfillflow_benchmark",
    )
    stopped: list[str] = []

    class FailedProcess:
        def wait(self, _timeout: float, **_kwargs: object) -> int:
            if failure == "timeout":
                raise CampaignExecutionError("external process exceeded its frozen timeout")
            return 7

        def ensure_stopped(self) -> None:
            stopped.append("process")

    class Sampler:
        failure = None

        def start(self) -> None:
            pass

        def stop(self) -> None:
            stopped.append("sampler")

    monkeypatch.setattr("benchmarks.run_campaign.ManagedProcess", lambda _argv: FailedProcess())
    monkeypatch.setattr("benchmarks.run_campaign.ResourceSampler", lambda *_a, **_k: Sampler())
    monkeypatch.setattr("benchmarks.run_campaign._wait_for_container_file", lambda *_a: None)
    monkeypatch.setattr(
        "benchmarks.run_campaign._terminate_container_phase", lambda *_a: stopped.append("phase")
    )
    monkeypatch.setattr(
        "benchmarks.run_campaign.run_capture",
        lambda *_a: subprocess.CompletedProcess([], 0, "", ""),
    )
    with pytest.raises(CampaignExecutionError, match=r"timeout|invalidated"):
        _run_phase(
            bundle,
            bundle.manifest.loads[0],
            "measurement",
            "http://app:8000",
            observed,
            object(),  # type: ignore[arg-type]
            "/runtime/campaign.json",
            tmp_path,
        )
    assert "process" in stopped and "sampler" in stopped
    assert "phase" in stopped
    assert (tmp_path / "phase-error.json").is_file()
    assert not (tmp_path / "locust_stats.csv").exists()


def _write_resources(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "timestamp_utc,service,container_id,cpu_percent,memory_usage_bytes,"
        "memory_limit_bytes,postgres_active_connections\n"
        "2026-09-04T00:00:00+00:00,app,app-id,1,10,100,\n"
        "2026-09-04T00:00:00+00:00,loadgen,loadgen-id,1,10,100,\n"
        "2026-09-04T00:00:00+00:00,postgres,postgres-id,1,10,100,2\n",
        encoding="utf-8",
    )


def test_stabilization_uses_injected_clock_and_wait_and_records_observed_duration() -> None:
    elapsed = 0.0
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        waits.append(seconds)
        elapsed += seconds + 0.25

    origin = datetime(2026, 9, 4, tzinfo=UTC)
    result = _stabilize(
        3,
        monotonic=lambda: elapsed,
        sleep=sleep,
        utc_now=lambda: origin + timedelta(seconds=elapsed),
    )
    assert waits == [3]
    assert result == {
        "expected_seconds": 3,
        "observed_seconds": 3.25,
        "started_at": origin.isoformat(),
        "finished_at": (origin + timedelta(seconds=3.25)).isoformat(),
    }
    waits.clear()
    assert _stabilize(0, monotonic=lambda: elapsed, sleep=sleep)["observed_seconds"] == 0
    assert waits == []


def test_repetition_requires_warmup_resource_artifact(tmp_path: Path) -> None:
    for name in (
        "locust_stats.csv",
        "locust_stats_history.csv",
        "locust_failures.csv",
        "locust_exceptions.csv",
        "response_codes.csv",
        "operational_results.csv",
        "resources.csv",
        "database_counts.csv",
        "metadata.json",
    ):
        (tmp_path / name).touch()
    with pytest.raises(CampaignExecutionError, match=r"warmup/resources\.csv"):
        _require_repetition_artifacts(tmp_path)
    _write_resources(tmp_path / "warmup" / "resources.csv")
    _require_repetition_artifacts(tmp_path)


@pytest.mark.parametrize("failure", [None, "identity", "dynamic"])
def test_execute_orders_preparation_stabilization_and_host_gates_for_every_repetition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str | None,
) -> None:
    from benchmarks import run_campaign as runner

    bundle = _bundle()
    loads = (bundle.manifest.loads[0], bundle.manifest.loads[0].model_copy(update={"name": "next"}))
    bundle = replace(
        bundle, manifest=bundle.manifest.model_copy(update={"repetitions": 2, "loads": loads})
    )
    order: list[str] = []
    identity = LogicalDatabaseIdentity(DatabaseDigests("0" * 64, {}), DatabaseDigests("1" * 64, {}))
    observed = ObservedEnvironment(
        {},
        {"app": "app-id", "postgres": "postgres-id", "loadgen": "loadgen-id"},
        "synthetic",
        "synthetic",
    )
    report = {"synthetic": {"expected": 1, "observed": 1, "matches": True}}
    stabilization = {
        "started_at": "synthetic-start",
        "finished_at": "synthetic-end",
        "observed_seconds": 0,
    }

    def step(name: str, value: object = None) -> object:
        order.append(name)
        return value

    class FakeHost:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def identity(self) -> object:
            step("identity")
            if failure == "identity":
                raise EnvironmentMismatchError({"os": {"matches": False}})
            return report

        def dynamic(self, _ids: object) -> object:
            step("dynamic")
            if failure == "dynamic":
                raise EnvironmentMismatchError({"ac_power": {"matches": False}})
            return report

    class FakeDocker:
        def __init__(self, *_a: object) -> None:
            pass

        def observe(self) -> object:
            return step("docker", observed)

    class FakeDatabase:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def snapshot(self, label: str) -> DatabaseSnapshot:
            return DatabaseSnapshot(label, {}, {}, {})

    def phase(*args: object) -> PhaseExecution:
        name = args[2]
        order.append(str(name))
        directory = args[-1]
        assert isinstance(directory, Path)
        if name == "warmup":
            _write_resources(directory / "warmup" / "resources.csv")
        else:
            _write_resources(directory / "resources.csv")
            for filename in (
                "locust_stats.csv",
                "locust_stats_history.csv",
                "locust_failures.csv",
                "locust_exceptions.csv",
                "response_codes.csv",
                "operational_results.csv",
            ):
                (directory / filename).touch()
        instant = datetime(2026, 9, 4, tzinfo=UTC)
        return PhaseExecution(name, instant, instant, 0)  # type: ignore[arg-type]

    monkeypatch.setattr(
        runner,
        "_git_provenance",
        lambda _p: GitProvenance(bundle.manifest.git_sha, "release/v1.0.0", True, True),
    )
    monkeypatch.setattr(runner, "HostProbe", FakeHost)
    monkeypatch.setattr(runner, "_project_release", lambda _p: bundle.manifest.release)
    monkeypatch.setattr(runner, "DockerProbe", FakeDocker)
    monkeypatch.setattr(runner, "DatabaseProbe", FakeDatabase)
    monkeypatch.setattr(runner, "_run_preparation", lambda *_a: step("prepare"))
    monkeypatch.setattr(
        runner,
        "_verify_initial_state",
        lambda *_a: step("verify", (DatabaseSnapshot("initial", {}, {}, {}), identity)),
    )
    monkeypatch.setattr(
        runner, "_install_runtime_manifest", lambda *_a: step("install", "/synthetic")
    )
    monkeypatch.setattr(runner, "_stabilize", lambda seconds: step("stabilize", stabilization))
    monkeypatch.setattr(runner, "_run_phase", phase)
    monkeypatch.setattr(runner, "_verify_warmup", lambda *_a: identity)
    monkeypatch.setattr(runner, "_verify_measurement", lambda *_a: None)
    monkeypatch.setattr(runner, "_merge_operational_results", lambda *_a: None)
    directory = tmp_path / "synthetic"
    if failure:
        with pytest.raises(EnvironmentMismatchError):
            _execute(bundle, FIXTURE, "http://synthetic", directory, ["synthetic"])
        assert "warmup" not in order
        path = (
            directory / "metadata.json"
            if failure == "identity"
            else directory / "synthetic-4-users-r01.partial" / "metadata.json"
        )
        metadata = json.loads(path.read_text(encoding="utf-8"))
        assert metadata["valid"] is False
        if failure == "dynamic":
            assert metadata["stabilization"] == stabilization
        assert (directory / ".incomplete.json").is_file()
    else:
        assert _execute(bundle, FIXTURE, "http://synthetic", directory, ["synthetic"]) == 0
        assert (
            order
            == ["identity"]
            + [
                "prepare",
                "docker",
                "verify",
                "install",
                "stabilize",
                "dynamic",
                "warmup",
                "measurement",
            ]
            * 4
        )
        for path in directory.glob("*-r??/metadata.json"):
            metadata = json.loads(path.read_text(encoding="utf-8"))
            assert metadata["host_identity"] == metadata["host_state"] == report
            assert metadata["stabilization"] == stabilization
            assert metadata["valid"] is True
        assert not (directory / ".incomplete.json").exists()


def test_validate_only_never_constructs_a_host_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from benchmarks import run_campaign as runner

    def forbidden(*_a: object, **_k: object) -> None:
        pytest.fail("validate-only must not inspect the host")

    monkeypatch.setattr(runner, "HostProbe", forbidden)
    monkeypatch.setattr(sys, "argv", ["campaign", "--manifest", str(FIXTURE), "--validate-only"])
    assert runner.main() == 0


def test_managed_process_uses_windows_process_group_and_bounded_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows_group_flag = 123
    windows_break_event = 456
    popen_kwargs: dict[str, object] = {}

    class FakeProcess:
        pid = 321

        def __init__(self) -> None:
            self.running = True
            self.wait_calls = 0
            self.signals: list[object] = []
            self.killed = False

        def poll(self) -> int | None:
            return None if self.running else 0

        def send_signal(self, event: object) -> None:
            self.signals.append(event)

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired("synthetic", timeout)
            self.running = False
            return 0

        def kill(self) -> None:
            self.killed = True

    fake_process = FakeProcess()

    def fake_popen(_command: list[str], **kwargs: object) -> FakeProcess:
        popen_kwargs.update(kwargs)
        return fake_process

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", windows_group_flag, raising=False)
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", windows_break_event, raising=False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    process = ManagedProcess(["safe-command"])
    process.terminate()

    assert popen_kwargs["creationflags"] == windows_group_flag
    assert popen_kwargs["start_new_session"] is False
    assert "shell" not in popen_kwargs
    assert fake_process.signals == [windows_break_event]
    assert fake_process.killed is True
    assert fake_process.running is False


def test_managed_process_uses_posix_session_and_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popen_kwargs: dict[str, object] = {}
    group_signals: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 654
        running = True

        def poll(self) -> int | None:
            return None if self.running else 0

        def wait(self, timeout: float | None = None) -> int:
            self.running = False
            return 0

    fake_process = FakeProcess()

    def fake_popen(_command: list[str], **kwargs: object) -> FakeProcess:
        popen_kwargs.update(kwargs)
        return fake_process

    def fake_killpg(pid: int, event: int) -> None:
        group_signals.append((pid, event))

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "killpg", fake_killpg, raising=False)

    process = ManagedProcess(["safe-command"])
    process.terminate()

    assert popen_kwargs["creationflags"] == 0
    assert popen_kwargs["start_new_session"] is True
    assert "shell" not in popen_kwargs
    assert group_signals == [(fake_process.pid, signal.SIGTERM)]


def test_managed_process_does_not_signal_completed_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_signals: list[tuple[int, int]] = []

    class CompletedProcess:
        pid = 987

        def poll(self) -> int:
            return 0

    completed_process = CompletedProcess()

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: completed_process)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, event: group_signals.append((pid, event)),
        raising=False,
    )

    ManagedProcess(["safe-command"]).terminate()

    assert group_signals == []


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
    monkeypatch.setattr("benchmarks.run_campaign._project_release", lambda _path: "v1.0.0")
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
    assert _project_release(Path.cwd()) == "v1.2.0.dev0"


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
        "duplicate-notification",
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
    elif mutation == "duplicate-notification":
        database.observations[event_id] = replace(
            observation, notification_count=2, matching_notification_count=2
        )
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
