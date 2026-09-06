"""End-to-end PostgreSQL 18 structural preflight through the production probe."""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from benchmarks.campaign import load_campaign
from benchmarks.collectors import DatabaseProbe, run_capture
from benchmarks.database_contract import STRUCTURAL_SCHEMA_QUERIES
from benchmarks.run_campaign import CampaignExecutionError, _verify_initial_state
from benchmarks.seed_loader import load_dataset

from fulfillflow.asyncio_support import run_async

pytestmark = pytest.mark.integration

FIXTURE = Path("benchmarks/fixtures/smoke-campaign.json")
POSTGRES_IMAGE = (
    "postgres:18-trixie@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280"
)
EXPECTED_SCHEMA_SHA256 = "0ee99170b78404380541ac12cf676126d047fc60275a4e2947fbe5a68b32652b"
EXPECTED_DATASET_SHA256 = "5897d7441f73fec77d98ff97196aff0becc3f301e45c708febff493d8f4a63bf"
POSTGRES_USER = "ff_schema_probe"
POSTGRES_DEFAULT_DATABASE = "ff_schema_bootstrap"
POSTGRES_PASSWORD = "task07-structural-test-only"
DOCKER_COMMAND_TIMEOUT_SECONDS = 120.0
POSTGRES_START_TIMEOUT_SECONDS = 120.0


@pytest.fixture(scope="module")
def structural_postgres() -> Iterator[_DockerPostgres]:
    """Provide one isolated container and a fresh target database per test case."""
    harness = _DockerPostgres.start()
    try:
        yield harness
    finally:
        harness.close()


def test_real_database_probe_accepts_exact_migrated_and_seeded_database(
    structural_postgres: _DockerPostgres,
) -> None:
    bundle = load_campaign(FIXTURE)
    with structural_postgres.provision_database("positive") as target:
        observed = target.probe.structural_schema_identity()

        assert observed.contract_version == bundle.manifest.database.structural_contract_version
        assert observed.sha256 == EXPECTED_SCHEMA_SHA256
        snapshot, identity = _verify_initial_state(bundle, target.probe)

        assert snapshot.label == "initial"
        assert identity.structural_schema is not None
        assert identity.structural_schema.expected == EXPECTED_SCHEMA_SHA256
        assert identity.structural_schema.observed == EXPECTED_SCHEMA_SHA256
        assert identity.structural_schema.matches is True
        _assert_real_probe_transport(target)


def test_real_event_observation_preserves_notification_uuid_and_counts(
    structural_postgres: _DockerPostgres,
) -> None:
    with structural_postgres.provision_database("notification-uuid") as target:
        row = target.probe.query_scalar(
            "SELECT i.external_event_id || '|' || n.id::text || '|' || e.id::text "
            "FROM notifications n JOIN tracking_events e ON e.id = n.tracking_event_id "
            "JOIN carrier_event_inbox i ON i.id = e.inbox_event_id ORDER BY n.id LIMIT 1"
        )
        event_id, notification_id, tracking_id = row.split("|")
        observed = target.probe.event_observations([event_id])[event_id]
        assert observed.notification_id == notification_id
        assert observed.tracking_event_id == tracking_id
        assert observed.notification_count == 1
        assert observed.matching_notification_count == 1
        assert observed.raw_body_sha256 == observed.payload_sha256
        before = target.probe.snapshot("before")
        # Real large SQL transport, without emitting workload or changing the seeded rows.
        missing_ids = [f"benchmark-warmup-missing-{index:064d}" for index in range(5159)]
        target.runner.records.clear()
        many = target.probe.event_observations([event_id, *missing_ids])
        assert many == {event_id: observed}
        record = target.runner.records[0]
        assert len(record.input_text or "") > 373_000
        assert len(subprocess.list2cmdline(record.argv)) < 1024
        assert "--file=-" in record.argv and "--interactive" in record.argv
        assert "--command" not in record.argv
        assert target.probe.snapshot("after").metrics() == before.metrics()
        assert target.probe.structural_schema_identity().sha256 == EXPECTED_SCHEMA_SHA256
        _assert_real_probe_transport(target)


@pytest.mark.parametrize(
    ("case", "ddl", "verification_sql", "verification_result"),
    [
        pytest.param(
            "type",
            "ALTER TABLE carriers ALTER COLUMN name TYPE text",
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'public.carriers'::regclass AND attname = 'name'",
            "text",
            id="compatible-type-preserving-reference-rows",
        ),
        pytest.param(
            "foreign-key",
            "ALTER TABLE shipments DROP CONSTRAINT fk_shipments_order_id_orders",
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'public.shipments'::regclass "
            "AND conname = 'fk_shipments_order_id_orders'",
            "0",
            id="foreign-key",
        ),
        pytest.param(
            "index",
            "DROP INDEX ix_orders_status_created_at",
            "SELECT to_regclass('public.ix_orders_status_created_at') IS NULL",
            "t",
            id="index",
        ),
    ],
)
def test_real_database_probe_refuses_committed_structural_drift_before_content(
    case: str,
    ddl: str,
    verification_sql: str,
    verification_result: str,
    structural_postgres: _DockerPostgres,
) -> None:
    bundle = load_campaign(FIXTURE)
    with structural_postgres.provision_database(case) as target:
        before_dataset = target.probe.frozen_content_identity(bundle.dataset_document)
        before_carriers = target.probe.official_carrier_identity()

        structural_postgres.execute_sql(target.database, ddl)

        assert structural_postgres.query_scalar(target.database, verification_sql) == (
            verification_result
        )
        assert target.probe.alembic_heads() == ("0004_notifications",)
        assert target.probe.frozen_content_identity(bundle.dataset_document) == before_dataset
        assert target.probe.official_carrier_identity() == before_carriers

        target.runner.records.clear()
        with pytest.raises(
            CampaignExecutionError,
            match="prepared database schema failed structural verification",
        ):
            _verify_initial_state(bundle, target.probe)

        _assert_real_probe_transport(target)
        executed_sql = [_command_sql(record.argv) for record in target.runner.records]
        assert not any("SELECT 'orders', count(*)" in sql for sql in executed_sql)
        assert not any("to_jsonb(row_data)" in sql for sql in executed_sql)


@dataclass(frozen=True, slots=True)
class _CommandRecord:
    argv: tuple[str, ...]
    output_line_count: int
    input_text: str | None = None


class _RecordingRealRunner:
    """Record sanitized command shape while still executing the production subprocess path."""

    def __init__(self) -> None:
        self.records: list[_CommandRecord] = []

    def __call__(
        self,
        command: list[str],
        timeout_seconds: float,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = run_capture(command, timeout_seconds, input_text)
        line_count = len([line for line in completed.stdout.splitlines() if line.strip()])
        self.records.append(_CommandRecord(tuple(command), line_count, input_text))
        return completed


@dataclass(frozen=True, slots=True)
class _ProvisionedDatabase:
    container: str
    user: str
    database: str
    probe: DatabaseProbe
    runner: _RecordingRealRunner


class _DockerPostgres:
    """Own exact, recoverable Docker resources for the structural integration tests."""

    def __init__(self, token: str) -> None:
        prefix = f"fulfillflow-task07-schema-e2e-{token}"
        self.container = f"{prefix}-postgres"
        self.network = f"{prefix}-network"
        self.volume = f"{prefix}-data"
        self.host_port: int | None = None
        self._network_created = False
        self._volume_created = False
        self._container_created = False

    @classmethod
    def start(cls) -> _DockerPostgres:
        harness = cls(uuid4().hex[:12])
        try:
            _docker(["network", "create", harness.network])
            harness._network_created = True
            _docker(["volume", "create", harness.volume])
            harness._volume_created = True
            _docker(
                [
                    "run",
                    "--detach",
                    "--name",
                    harness.container,
                    "--network",
                    harness.network,
                    "--mount",
                    f"type=volume,source={harness.volume},target=/var/lib/postgresql",
                    "--publish",
                    "127.0.0.1::5432",
                    "--label",
                    "fulfillflow.test=task07-structural-database-probe",
                    "--env",
                    f"POSTGRES_USER={POSTGRES_USER}",
                    "--env",
                    f"POSTGRES_DB={POSTGRES_DEFAULT_DATABASE}",
                    "--env",
                    "POSTGRES_PASSWORD",
                    POSTGRES_IMAGE,
                ],
                environment={"POSTGRES_PASSWORD": POSTGRES_PASSWORD},
                timeout_seconds=180.0,
            )
            harness._container_created = True
            harness._wait_until_ready()
            published = _docker(["port", harness.container, "5432/tcp"]).stdout.strip()
            match = re.search(r":([0-9]+)$", published)
            if match is None:
                raise AssertionError("Docker did not publish the isolated PostgreSQL port")
            harness.host_port = int(match.group(1))
            return harness
        except Exception:
            harness.close()
            raise

    @contextmanager
    def provision_database(self, case: str) -> Iterator[_ProvisionedDatabase]:
        suffix = re.sub(r"[^a-z0-9]+", "_", case.casefold()).strip("_")
        database = f"ff_schema_target_{suffix}_{uuid4().hex[:8]}"
        if database in {POSTGRES_DEFAULT_DATABASE, POSTGRES_USER}:
            raise AssertionError("target database must differ from defaults")
        try:
            self.execute_sql(
                POSTGRES_DEFAULT_DATABASE,
                f'CREATE DATABASE "{database}" OWNER "{POSTGRES_USER}"',
            )
            database_url = self._database_url(database)
            with patch.dict(os.environ, {"DATABASE_URL": database_url}):
                alembic_command.upgrade(Config("alembic.ini"), "head")
            result = run_async(
                load_dataset(
                    "benchmark",
                    database_url=database_url,
                    app_env="test",
                    confirmed_database_name=database,
                )
            )
            assert result.inserted is True
            assert result.logical_hash == EXPECTED_DATASET_SHA256
            assert (
                self.query_scalar(
                    POSTGRES_DEFAULT_DATABASE,
                    "SELECT to_regclass('public.alembic_version') IS NULL",
                )
                == "t"
            )

            runner = _RecordingRealRunner()
            probe = DatabaseProbe(
                self.container,
                POSTGRES_USER,
                database,
                command_runner=runner,
                input_command_runner=runner,
                timeout_seconds=30.0,
            )
            yield _ProvisionedDatabase(
                self.container,
                POSTGRES_USER,
                database,
                probe,
                runner,
            )
        finally:
            self.execute_sql(
                POSTGRES_DEFAULT_DATABASE,
                f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)',
            )

    def execute_sql(self, database: str, sql: str) -> None:
        _docker(
            [
                "exec",
                self.container,
                "psql",
                "--no-psqlrc",
                "--username",
                POSTGRES_USER,
                "--dbname",
                database,
                "--set=ON_ERROR_STOP=1",
                "--command",
                sql,
            ]
        )

    def query_scalar(self, database: str, sql: str) -> str:
        completed = _docker(
            [
                "exec",
                self.container,
                "psql",
                "--no-psqlrc",
                "--username",
                POSTGRES_USER,
                "--dbname",
                database,
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                "--command",
                sql,
            ]
        )
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise AssertionError("isolated PostgreSQL scalar query returned an invalid row count")
        return lines[0]

    def close(self) -> None:
        failures: list[str] = []
        if self._container_created:
            completed = _docker(
                ["rm", "--force", "--volumes", self.container],
                check=False,
            )
            if completed.returncode != 0:
                failures.append("container")
            self._container_created = False
        if self._volume_created:
            completed = _docker(["volume", "rm", self.volume], check=False)
            if completed.returncode != 0:
                failures.append("volume")
            self._volume_created = False
        if self._network_created:
            completed = _docker(["network", "rm", self.network], check=False)
            if completed.returncode != 0:
                failures.append("network")
            self._network_created = False
        if failures:
            raise AssertionError(
                "failed to remove isolated structural test resources: " + ", ".join(failures)
            )

    def _database_url(self, database: str) -> str:
        if self.host_port is None:
            raise AssertionError("isolated PostgreSQL port was not discovered")
        return (
            f"postgresql+psycopg://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
            f"@127.0.0.1:{self.host_port}/{database}"
        )

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + POSTGRES_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            completed = _docker(
                [
                    "exec",
                    self.container,
                    "pg_isready",
                    # The initialization server accepts sockets before its shutdown.
                    "--host=127.0.0.1",
                    "--username",
                    POSTGRES_USER,
                    "--dbname",
                    POSTGRES_DEFAULT_DATABASE,
                ],
                check=False,
                timeout_seconds=10.0,
            )
            if completed.returncode == 0:
                return
            time.sleep(0.25)
        raise AssertionError("isolated PostgreSQL 18 did not become ready before timeout")


def _assert_real_probe_transport(target: _ProvisionedDatabase) -> None:
    psql_records = [
        record
        for record in target.runner.records
        if record.argv[:4] == ("docker", "exec", target.container, "psql")
    ]
    assert psql_records
    for record in psql_records:
        command = record.argv
        assert command[command.index("--username") + 1] == target.user
        assert command[command.index("--dbname") + 1] == target.database
        assert "--no-psqlrc" in command
        assert "--tuples-only" in command
        assert "--no-align" in command
        assert "--field-separator=|" in command
        assert "--set=ON_ERROR_STOP=1" in command
        assert "--command" in command
        assert "--password" not in command
        assert "-W" not in command

    structural_sql = set(STRUCTURAL_SCHEMA_QUERIES.values())
    structural_records = [
        record for record in psql_records if _command_sql(record.argv) in structural_sql
    ]
    assert len(structural_records) >= len(structural_sql)
    assert any(record.output_line_count > 1 for record in structural_records)


def _command_sql(command: tuple[str, ...]) -> str:
    return command[command.index("--command") + 1]


def _docker(
    arguments: list[str],
    *,
    check: bool = True,
    environment: dict[str, str] | None = None,
    timeout_seconds: float = DOCKER_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command = ["docker", *arguments]
    process_environment = os.environ.copy()
    process_environment.update(environment or {})
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
            env=process_environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError(f"Docker {arguments[0]} did not complete safely") from exc
    if check and completed.returncode != 0:
        raise AssertionError(f"Docker {arguments[0]} failed for the isolated structural test")
    return completed
