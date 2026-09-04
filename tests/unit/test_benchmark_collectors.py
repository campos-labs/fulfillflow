"""Discriminative external environment, resource, and database artifact tests."""

from __future__ import annotations

import json
import subprocess
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


class _FakeDatabase:
    def active_connections(self) -> int:
        return 7


def _stats_runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    del timeout
    assert command[:3] == ["docker", "stats", "--no-stream"]
    payload = json.dumps({"CPUPerc": "1.5%", "MemUsage": "10MiB / 512MiB"})
    return subprocess.CompletedProcess(command, 0, payload, "")


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
