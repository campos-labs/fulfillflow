"""Discriminative external environment, resource, and database artifact tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from itertools import count
from pathlib import Path

import pytest
from benchmarks.campaign import load_campaign
from benchmarks.collectors import (
    DatabaseProbe,
    DatabaseSnapshot,
    DockerProbe,
    EnvironmentMismatchError,
    ExternalCommandError,
    ResourceSampler,
    _normalize_finite_number,
    _resource_delta,
    run_capture,
    validate_resource_samples,
    write_database_counts,
)
from benchmarks.database_contract import (
    STRUCTURAL_SCHEMA_QUERIES,
    STRUCTURAL_SCHEMA_TABLES,
    DatabaseIdentityError,
    structural_schema_identity,
)

FIXTURE = Path("benchmarks/fixtures/smoke-campaign.json")


def test_environment_verification_records_expected_observed_and_match() -> None:
    bundle = load_campaign(FIXTURE)
    probe = DockerProbe(bundle, Path.cwd(), command_runner=_docker_runner())

    observed = probe.observe()

    assert observed.container_ids == {
        "app": "app-id",
        "postgres": "postgres-id",
        "loadgen": "loadgen-id",
    }
    assert observed.checks["app.cpus"] == {
        "expected": 1.0,
        "observed": 1.0,
        "matches": True,
    }
    assert observed.checks["app.environment.OTEL_TRACES_SAMPLER_ARG"] == {
        "expected": 0.0,
        "observed": "0.0",
        "expected_normalized": "0",
        "observed_normalized": "0",
        "matches": True,
    }
    assert all(item["matches"] is True for item in observed.checks.values())
    serialized = json.dumps(observed.checks)
    assert "POSTGRES_PASSWORD" not in serialized
    assert "secret" not in serialized.casefold()


def test_environment_verification_rejects_effective_resource_drift() -> None:
    bundle = load_campaign(FIXTURE)
    probe = DockerProbe(bundle, Path.cwd(), command_runner=_docker_runner(app_cpus=2.0))

    with pytest.raises(EnvironmentMismatchError) as captured:
        probe.observe()

    assert captured.value.report["app.cpus"]["matches"] is False


def test_environment_verification_rejects_different_tracing_sampling() -> None:
    bundle = load_campaign(FIXTURE)
    probe = DockerProbe(
        bundle,
        Path.cwd(),
        command_runner=_docker_runner(tracing_sampling="0.1"),
    )

    with pytest.raises(EnvironmentMismatchError) as captured:
        probe.observe()

    check = captured.value.report["app.environment.OTEL_TRACES_SAMPLER_ARG"]
    assert check["observed"] == "0.1"
    assert check["observed_normalized"] == "0.1"
    assert check["matches"] is False


@pytest.mark.parametrize("value", [None, True, False, "NaN", "Infinity", "-inf", "text"])
def test_semantic_number_rejects_missing_boolean_or_nonfinite_values(value: object) -> None:
    assert _normalize_finite_number(value) is None


@pytest.mark.parametrize("value", ["0", "0.0", "0.00"])
def test_semantic_number_treats_equivalent_zero_text_as_equal(value: str) -> None:
    assert _normalize_finite_number(value) == "0"


def test_resource_sampler_writes_all_services_and_postgres_connections(tmp_path: Path) -> None:
    runner = _stats_runner
    database = _FakeDatabase()
    sampler = ResourceSampler(
        tmp_path / "resources.csv",
        {"app": "app-id", "postgres": "postgres-id", "loadgen": "loadgen-id"},
        database,  # type: ignore[arg-type]
        0.01,
        command_runner=runner,
        command_timeout_seconds=1,
    )

    sampler.start()
    sampler.stop()
    validate_resource_samples(sampler.output_path, sampler.container_ids)

    content = (tmp_path / "resources.csv").read_text(encoding="utf-8")
    assert "app,app-id,1.5,10485760,536870912," in content
    assert "postgres,postgres-id,1.5,10485760,536870912,7" in content
    assert "loadgen,loadgen-id,1.5,10485760,536870912," in content


def test_resource_sampler_fails_closed_when_a_mandatory_sample_cannot_be_obtained(
    tmp_path: Path,
) -> None:
    def failed_stats(_command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        raise ExternalCommandError("mandatory synthetic sample failed")

    sampler = ResourceSampler(
        tmp_path / "resources.csv",
        {"app": "app-id", "postgres": "postgres-id", "loadgen": "loadgen-id"},
        _FakeDatabase(),  # type: ignore[arg-type]
        0.01,
        command_runner=failed_stats,
        command_timeout_seconds=0.1,
    )

    sampler.start()
    with pytest.raises(ExternalCommandError, match="mandatory resource sampling failed"):
        sampler.stop()


@pytest.mark.parametrize(
    "defect",
    ["empty", "header", "truncated", "duplicate", "nan", "container", "memory", "connections"],
)
def test_resource_completeness_rejects_missing_cycles_and_invalid_rows(
    tmp_path: Path, defect: str
) -> None:
    path = tmp_path / "resources.csv"
    ids = {"app": "app-id", "postgres": "postgres-id", "loadgen": "loadgen-id"}
    sampler = ResourceSampler(path, ids, _FakeDatabase(), 1, command_runner=_stats_runner)  # type: ignore[arg-type]
    sampler.start()
    sampler.stop()
    lines = path.read_text(encoding="utf-8").splitlines()
    if defect == "empty":
        lines = []
    elif defect == "header":
        lines = lines[:1]
    elif defect == "truncated":
        lines = lines[:-1]
    elif defect == "duplicate":
        lines.append(lines[-1])
    else:
        substitutions = {
            "nan": ("1.5", "nan"),
            "container": ("app-id", "foreign-id"),
            "memory": ("10485760", "-1"),
            "connections": (",7", ",-1"),
        }
        old, new = substitutions[defect]
        lines = [line.replace(old, new) for line in lines]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ExternalCommandError, match="resource CSV"):
        validate_resource_samples(path, ids)


def test_database_counts_contains_snapshots_and_explicit_deltas(tmp_path: Path) -> None:
    initial = DatabaseSnapshot(
        "initial",
        {"tracking_events": 15_000, "notifications": 2_998},
        {"PROCESSED": 15_000},
        {"APPLIED": 2_998},
    )
    final = DatabaseSnapshot(
        "post_measurement",
        {"tracking_events": 15_010, "notifications": 3_008},
        {"PROCESSED": 15_010},
        {"APPLIED": 3_008},
    )

    write_database_counts(tmp_path / "database_counts.csv", [initial, final])

    content = (tmp_path / "database_counts.csv").read_text(encoding="utf-8")
    assert "initial,table.tracking_events,15000" in content
    assert "post_measurement,table.notifications,3008" in content
    assert "delta:initial->post_measurement,tracking_result.APPLIED,10" in content


def test_database_probe_uses_every_frozen_pg_catalog_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sections = {
        "tables": [
            {
                "schema": "public",
                "name": name,
                "kind": "table",
                "row_security": False,
                "force_row_security": False,
            }
            for name in STRUCTURAL_SCHEMA_TABLES
        ],
        "columns": [],
        "constraints": [],
        "indexes": [],
    }
    by_query = {query: sections[section] for section, query in STRUCTURAL_SCHEMA_QUERIES.items()}
    observed_queries: list[str] = []
    probe = DatabaseProbe("postgres-id", "user", "database")

    def json_rows(query: str) -> list[dict[str, object]]:
        observed_queries.append(query)
        return by_query[query]

    monkeypatch.setattr(probe, "_json_rows", json_rows)

    assert probe.structural_schema_identity() == structural_schema_identity(sections)
    assert observed_queries == list(STRUCTURAL_SCHEMA_QUERIES.values())


def test_database_probe_rejects_malformed_psql_json() -> None:
    def malformed_runner(
        command: list[str],
        _timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "not-json\n", "")

    probe = DatabaseProbe("postgres-id", "user", "target_database", command_runner=malformed_runner)

    with pytest.raises(ExternalCommandError, match="invalid logical JSON row"):
        probe.structural_schema_identity()


def test_database_probe_rejects_a_structural_row_discarded_by_transport() -> None:
    sections = {
        "tables": [
            {
                "schema": "public",
                "name": name,
                "kind": "table",
                "row_security": False,
                "force_row_security": False,
            }
            for name in STRUCTURAL_SCHEMA_TABLES[:-1]
        ],
        "columns": [],
        "constraints": [],
        "indexes": [],
    }

    def incomplete_runner(
        command: list[str],
        _timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        sql = command[command.index("--command") + 1]
        section = next(name for name, query in STRUCTURAL_SCHEMA_QUERIES.items() if query == sql)
        stdout = "".join(json.dumps(row) + "\n" for row in sections[section])
        return subprocess.CompletedProcess(command, 0, stdout, "")

    probe = DatabaseProbe(
        "postgres-id", "user", "target_database", command_runner=incomplete_runner
    )

    with pytest.raises(DatabaseIdentityError, match="table-set"):
        probe.structural_schema_identity()


def test_nonzero_psql_exit_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive = "postgresql+psycopg://user:password@host/db raw_body=private"

    def failed_process(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, "", sensitive)

    monkeypatch.setattr(subprocess, "run", failed_process)

    with pytest.raises(ExternalCommandError) as captured:
        run_capture(["docker", "exec", "postgres-id", "psql"], 1.0)

    message = str(captured.value)
    assert message == "external command failed: docker exec"
    assert sensitive not in message


@pytest.mark.parametrize("event_count", [24, 5160])
def test_event_query_uses_bounded_argv_and_complete_stdin(event_count: int) -> None:
    event_ids = [f"benchmark-warmup-{index:064d}" for index in range(event_count)]
    calls: list[tuple[list[str], float, str]] = []

    def input_runner(
        command: list[str], timeout: float, sql: str
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, timeout, sql))
        assert command[:4] == ["docker", "exec", "--interactive", "postgres-id"]
        assert command[-1] == "--file=-"
        assert "--command" not in command
        assert "--set=ON_ERROR_STOP=1" in command
        assert len(subprocess.list2cmdline(command)) < 1024
        assert timeout == 7.0
        assert all(sql.count(f"'{event_id}'") == 1 for event_id in event_ids)
        assert "min(n.id::text)" in sql
        assert "count(n.id)" in sql
        assert sql.count("SELECT ") == 1
        # Exercise the real OS pipe without Docker, a shell, or sensitive output.
        digest = run_capture(
            [
                sys.executable,
                "-B",
                "-c",
                "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())",
            ],
            timeout,
            sql,
        ).stdout.strip()
        assert digest == hashlib.sha256(sql.encode("utf-8")).hexdigest()
        return subprocess.CompletedProcess(command, 0, "", "")

    probe = DatabaseProbe(
        "postgres-id",
        "user",
        "target_database",
        input_command_runner=input_runner,
        timeout_seconds=7.0,
    )
    assert probe.event_observations(event_ids) == {}
    assert len(calls) == 1
    if event_count == 5160:
        assert len(calls[0][2]) > 373_000
    assert probe.event_observations([]) == {}
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_stdin_query_failures_preserve_timeout_and_sanitization(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    sensitive = "synthetic-secret raw_body=private database-url-private"
    calls: list[dict[str, object]] = []

    def process(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        assert kwargs["timeout"] == 7.0
        assert kwargs["shell"] is False
        assert kwargs["input"] and sensitive in str(kwargs["input"])
        assert sensitive not in " ".join(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 7.0, sensitive, sensitive)
        if failure == "oserror":
            raise OSError(sensitive)
        return subprocess.CompletedProcess(command, 2, sensitive, sensitive)

    monkeypatch.setattr(subprocess, "run", process)
    probe = DatabaseProbe("postgres-id", "user", "target_database", timeout_seconds=7.0)
    with pytest.raises(ExternalCommandError) as captured:
        probe.event_observations([sensitive])
    assert len(calls) == 1
    assert sensitive not in str(captured.value)
    assert "docker exec" in str(captured.value)


@pytest.mark.parametrize("output", ["not-json", "{}\n{}"])
def test_stdin_event_query_rejects_invalid_identity_output(output: str) -> None:
    def input_runner(
        command: list[str], _timeout: float, _sql: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, output, "")

    probe = DatabaseProbe(
        "postgres-id", "user", "target_database", input_command_runner=input_runner
    )
    with pytest.raises(ExternalCommandError, match="invalid"):
        probe.event_observations(["synthetic-event"])


class _FakeDatabase:
    def active_connections(self) -> int:
        return 7


_stats_sequence = count(1)


def _stats_runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    del timeout
    if command[:3] == ["docker", "context", "inspect"]:
        return subprocess.CompletedProcess(command, 0, "unix:///var/run/docker.sock", "")
    assert command[1:4] == ["-B", "-m", "benchmarks.resource_snapshot"]
    index = next(_stats_sequence)
    payload = json.dumps(
        {
            identifier: {
                "read": f"{index:020}",
                "cpu": index * 3,
                "system": index * 400,
                "cpus": 2,
                "memory": 10485760,
                "limit": 536870912,
            }
            for identifier in command[5:]
        }
    )
    return subprocess.CompletedProcess(command, 0, payload, "")


def test_resource_delta_requires_fresh_counters_and_preserves_docker_cpu_scale() -> None:
    old = {
        "read": "2026-09-04T00:00:00Z",
        "cpu": 100,
        "system": 1000,
        "cpus": 8,
        "memory": 100,
        "limit": 1000,
    }
    new = dict(old, read="2026-09-04T00:00:01Z", cpu=200, system=1800)
    assert _resource_delta(old, new) == (100.0, 100, 1000)
    with pytest.raises(ValueError, match="advance"):
        _resource_delta(old, old)
    with pytest.raises(ValueError, match="delta"):
        _resource_delta(old, dict(new, cpu=99))


@pytest.mark.parametrize(("query_seconds", "expected_wait"), [(0.3, 0.7), (1.4, 0.6)])
def test_sampler_schedules_deadlines_without_adding_query_time_or_catchup_bursts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query_seconds: float,
    expected_wait: float,
) -> None:
    clock = [0.0]
    waits: list[float] = []
    monkeypatch.setattr("benchmarks.collectors.time.monotonic", lambda: clock[0])

    class Stop:
        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            clock[0] += seconds
            return len(waits) == 3

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        clock[0] += query_seconds
        return _stats_runner(command, timeout)

    sampler = ResourceSampler(
        tmp_path / "resources.csv",
        {"postgres": "postgres-id"},
        _FakeDatabase(),
        1,
        command_runner=runner,
    )  # type: ignore[arg-type]
    sampler._stop = Stop()  # type: ignore[assignment]
    sampler._run()
    assert sampler.error is None
    assert waits == pytest.approx([expected_wait] * 3)


def _docker_runner(*, app_cpus: float = 1.0, tracing_sampling: str = "0.0"):
    digests = {
        "app-id": "sha256:" + ("0" * 64),
        "postgres-id": "sha256:" + ("1" * 64),
        "loadgen-id": "sha256:" + ("2" * 64),
    }

    def run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        del timeout
        if "compose" in command and "ps" in command:
            service = command[-1]
            identifier = {"app": "app-id", "db": "postgres-id", "loadgen": "loadgen-id"}[service]
            return subprocess.CompletedProcess(command, 0, identifier + "\n", "")
        if command[:2] == ["docker", "inspect"]:
            identifier = command[-1]
            service = {"app-id": "app", "postgres-id": "db", "loadgen-id": "loadgen"}[identifier]
            environment = []
            command_line = ["python"]
            if service == "app":
                environment = [
                    "DB_POOL_SIZE=5",
                    "DB_MAX_OVERFLOW=0",
                    "DB_POOL_TIMEOUT_SECONDS=5",
                    "DB_STATEMENT_TIMEOUT_MS=5000",
                    "LOG_LEVEL=WARNING",
                    "LOG_FORMAT=json",
                    "OTEL_ENABLED=false",
                    f"OTEL_TRACES_SAMPLER_ARG={tracing_sampling}",
                    "SESSION_SECRET=must-not-leak",
                ]
                command_line = ["python", "-m", "uvicorn", "--workers", "1"]
            elif service == "db":
                environment = [
                    "POSTGRES_USER=fulfillflow",
                    "POSTGRES_DB=fulfillflow_benchmark",
                    "POSTGRES_PASSWORD=must-not-leak",
                ]
            nano_cpus = int((app_cpus if service == "app" else 1.0) * 1_000_000_000)
            document = [
                {
                    "Image": digests[identifier],
                    "Config": {
                        "Entrypoint": [],
                        "Cmd": command_line,
                        "Env": environment,
                        "Labels": {
                            "com.docker.compose.project": "fulfillflow-benchmark",
                            "com.docker.compose.service": service,
                        },
                    },
                    "State": {"Health": {"Status": "healthy"}},
                    "HostConfig": {"NanoCpus": nano_cpus, "Memory": 512 * 1024 * 1024},
                }
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(document), "")
        if command[:3] == ["docker", "image", "inspect"]:
            digest = command[-1]
            document = [{"Id": digest, "RepoDigests": [f"synthetic@{digest}"]}]
            return subprocess.CompletedProcess(command, 0, json.dumps(document), "")
        if command[:3] == ["docker", "exec", "postgres-id"]:
            return subprocess.CompletedProcess(command, 0, "180000\n", "")
        raise AssertionError(f"unexpected external command: {command[:3]}")

    return run
