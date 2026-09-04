"""External Docker/psql probes and phase-separated resource collection."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

from benchmarks.campaign import CampaignBundle
from benchmarks.database_contract import (
    BUSINESS_TABLES,
    STRUCTURAL_SCHEMA_QUERIES,
    DatabaseDigests,
    StructuralSchemaIdentity,
    artifact_business_rows,
    compare_database_content,
    official_carrier_rows,
    structural_schema_identity,
)

CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


class ExternalCommandError(RuntimeError):
    """Sanitized external probe failure without stdout, environment or credentials."""


class EnvironmentMismatchError(RuntimeError):
    """Raised when observed containers do not match the frozen campaign contract."""

    def __init__(self, report: dict[str, dict[str, object]]) -> None:
        self.report = report
        failed = sorted(name for name, value in report.items() if value["matches"] is False)
        super().__init__(f"observed benchmark environment diverges: {', '.join(failed)}")


@dataclass(frozen=True, slots=True)
class ObservedEnvironment:
    checks: dict[str, dict[str, object]]
    container_ids: dict[str, str]
    postgres_user: str
    postgres_database: str


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    label: str
    counts: dict[str, int]
    inbox_statuses: dict[str, int]
    tracking_results: dict[str, int]

    def metrics(self) -> dict[str, int]:
        values = {f"table.{key}": value for key, value in self.counts.items()}
        values.update({f"inbox_status.{key}": value for key, value in self.inbox_statuses.items()})
        values.update(
            {f"tracking_result.{key}": value for key, value in self.tracking_results.items()}
        )
        return dict(sorted(values.items()))


@dataclass(frozen=True, slots=True)
class EventObservation:
    external_event_id: str
    inbox_id: str
    inbox_status: str
    inbox_carrier_id: str
    inbox_received_at: str
    inbox_processed_at: str
    payload_sha256: str
    raw_body_sha256: str
    parsed_payload: object
    tracking_event_id: str
    event_received_at: str
    shipment_id: str
    event_carrier_id: str
    external_status: str
    canonical_status: str
    occurred_at: str
    application_result: str
    previous_shipment_status: str
    resulting_shipment_status: str
    notification_id: str
    notification_count: int
    matching_notification_count: int


class DockerProbe:
    """Observe only approved Compose services without retaining secret environment values."""

    def __init__(
        self,
        bundle: CampaignBundle,
        repository_root: Path,
        *,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.bundle = bundle
        self.repository_root = repository_root
        self.command_runner = command_runner or run_capture
        compose = Path(bundle.manifest.environment.compose_file)
        self.compose_file = (
            compose if compose.is_absolute() else (repository_root / compose).resolve()
        )

    def observe(self) -> ObservedEnvironment:
        manifest = self.bundle.manifest
        service_contract = manifest.environment.services
        service_names = {
            "app": service_contract.app,
            "postgres": service_contract.postgres,
            "loadgen": service_contract.loadgen,
        }
        container_ids = {
            name: self._container_id(service) for name, service in service_names.items()
        }
        inspections = {
            name: self._inspect_container(identifier) for name, identifier in container_ids.items()
        }
        app_env = _environment_map(inspections["app"])
        postgres_env = _environment_map(inspections["postgres"])
        postgres_user = _required_nonsecret_environment(postgres_env, "POSTGRES_USER")
        postgres_database = _required_nonsecret_environment(postgres_env, "POSTGRES_DB")
        checks: dict[str, dict[str, object]] = {}

        for name, service in service_names.items():
            inspection = inspections[name]
            labels = inspection.get("Config", {}).get("Labels", {}) or {}
            _add_check(
                checks,
                f"{name}.compose_project",
                manifest.environment.compose_project,
                labels.get("com.docker.compose.project"),
            )
            _add_check(
                checks,
                f"{name}.compose_service",
                service,
                labels.get("com.docker.compose.service"),
            )
            health = inspection.get("State", {}).get("Health", {}).get("Status")
            _add_check(checks, f"{name}.health", "healthy", health)
            candidates = self._image_digest_candidates(inspection)
            expected_digest = getattr(manifest.images, name)
            _add_check(
                checks,
                f"{name}.image_digest",
                expected_digest,
                sorted(candidates),
                matches=expected_digest in candidates,
            )
            host_config = inspection.get("HostConfig", {})
            observed_cpus = float(host_config.get("NanoCpus", 0)) / 1_000_000_000
            observed_memory = int(host_config.get("Memory", 0))
            expected_resource = getattr(manifest.resources, name)
            _add_check(
                checks,
                f"{name}.cpus",
                float(expected_resource.cpus),
                observed_cpus,
            )
            _add_check(
                checks,
                f"{name}.memory_bytes",
                _parse_memory_bytes(expected_resource.memory),
                observed_memory,
            )

        command = [
            *(_string_list(inspections["app"].get("Config", {}).get("Entrypoint"))),
            *(_string_list(inspections["app"].get("Config", {}).get("Cmd"))),
        ]
        observed_workers = _option_value(command, "--workers")
        _add_check(checks, "app.workers", str(manifest.workers), observed_workers)
        expected_app_env = {
            "DB_POOL_SIZE": str(manifest.pool.size),
            "DB_MAX_OVERFLOW": str(manifest.pool.max_overflow),
            "DB_STATEMENT_TIMEOUT_MS": str(manifest.pool.statement_timeout_ms),
            "LOG_LEVEL": manifest.telemetry.log_level,
            "LOG_FORMAT": manifest.telemetry.log_format,
            "OTEL_ENABLED": str(manifest.telemetry.tracing_enabled).lower(),
        }
        for key, expected in expected_app_env.items():
            _add_check(checks, f"app.environment.{key}", expected, app_env.get(key))
        _add_numeric_check(
            checks,
            "app.environment.DB_POOL_TIMEOUT_SECONDS",
            manifest.pool.timeout_seconds,
            app_env.get("DB_POOL_TIMEOUT_SECONDS"),
        )
        _add_numeric_check(
            checks,
            "app.environment.OTEL_TRACES_SAMPLER_ARG",
            manifest.telemetry.tracing_sampling,
            app_env.get("OTEL_TRACES_SAMPLER_ARG"),
        )

        database = DatabaseProbe(
            container_ids["postgres"],
            postgres_user,
            postgres_database,
            command_runner=self.command_runner,
            timeout_seconds=manifest.timeouts.command_seconds,
        )
        _add_check(checks, "postgres.major", 18, database.server_major())
        if any(not value["matches"] for value in checks.values()):
            raise EnvironmentMismatchError(checks)
        return ObservedEnvironment(checks, container_ids, postgres_user, postgres_database)

    def _compose_prefix(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.bundle.manifest.environment.compose_project,
            "--file",
            str(self.compose_file),
        ]

    def _container_id(self, service: str) -> str:
        completed = self.command_runner(
            [*self._compose_prefix(), "ps", "--quiet", service],
            self.bundle.manifest.timeouts.command_seconds,
        )
        identifier = completed.stdout.strip()
        if not identifier or "\n" in identifier:
            raise ExternalCommandError(
                f"expected exactly one running container for service {service}"
            )
        return identifier

    def _inspect_container(self, identifier: str) -> dict[str, Any]:
        completed = self.command_runner(
            ["docker", "inspect", identifier], self.bundle.manifest.timeouts.command_seconds
        )
        try:
            payload = json.loads(completed.stdout)
            inspection = payload[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise ExternalCommandError(
                "docker inspect returned an invalid container document"
            ) from exc
        if not isinstance(inspection, dict):
            raise ExternalCommandError("docker inspect returned an invalid container document")
        return inspection

    def _image_digest_candidates(self, inspection: dict[str, Any]) -> set[str]:
        candidates = {str(inspection.get("Image", ""))}
        image_reference = inspection.get("Image")
        if image_reference:
            completed = self.command_runner(
                ["docker", "image", "inspect", str(image_reference)],
                self.bundle.manifest.timeouts.command_seconds,
            )
            try:
                image = json.loads(completed.stdout)[0]
            except (json.JSONDecodeError, IndexError, TypeError) as exc:
                raise ExternalCommandError("docker image inspect returned invalid JSON") from exc
            candidates.add(str(image.get("Id", "")))
            for repo_digest in image.get("RepoDigests") or []:
                if "@" in repo_digest:
                    candidates.add(repo_digest.rsplit("@", maxsplit=1)[1])
        return {item for item in candidates if re.fullmatch(r"sha256:[0-9a-f]{64}", item)}


class DatabaseProbe:
    """Read benchmark state through psql inside the approved PostgreSQL container."""

    def __init__(
        self,
        container_id: str,
        user: str,
        database: str,
        *,
        command_runner: CommandRunner | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self.container_id = container_id
        self.user = user
        self.database = database
        self.command_runner = command_runner or run_capture
        self.timeout_seconds = timeout_seconds

    def server_major(self) -> int:
        value = self.query_scalar("SELECT current_setting('server_version_num')::integer")
        return int(value) // 10_000

    def active_connections(self) -> int:
        return int(
            self.query_scalar(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            )
        )

    def alembic_heads(self) -> tuple[str, ...]:
        """Read the exact installed Alembic heads without trusting a preparation command."""
        lines = _output_lines(
            self._psql("SELECT version_num FROM public.alembic_version ORDER BY version_num")
        )
        if not lines or len(set(lines)) != len(lines):
            raise ExternalCommandError("PostgreSQL returned an invalid Alembic head set")
        return tuple(lines)

    def structural_schema_identity(self) -> StructuralSchemaIdentity:
        """Hash the stable observed pg_catalog structure for the current release."""
        sections = {
            section: self._json_rows(query) for section, query in STRUCTURAL_SCHEMA_QUERIES.items()
        }
        return structural_schema_identity(sections)

    def snapshot(self, label: str) -> DatabaseSnapshot:
        counts = self.query_pairs(
            "SELECT 'orders', count(*) FROM orders UNION ALL "
            "SELECT 'shipments', count(*) FROM shipments UNION ALL "
            "SELECT 'carrier_event_inbox', count(*) FROM carrier_event_inbox UNION ALL "
            "SELECT 'tracking_events', count(*) FROM tracking_events UNION ALL "
            "SELECT 'notifications', count(*) FROM notifications ORDER BY 1"
        )
        inbox_statuses = self.query_pairs(
            "SELECT status::text, count(*) FROM carrier_event_inbox GROUP BY status ORDER BY status"
        )
        tracking_results = self.query_pairs(
            "SELECT application_result::text, count(*) FROM tracking_events "
            "GROUP BY application_result ORDER BY application_result"
        )
        return DatabaseSnapshot(label, counts, inbox_statuses, tracking_results)

    def frozen_content_identity(
        self,
        document: Mapping[str, object],
        *,
        allow_additional_rows: bool = False,
        ignored_columns_by_row: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
    ) -> DatabaseDigests:
        """Compare every seeded column with the authenticated integral artifact."""
        expected = artifact_business_rows(document)
        observed = self.business_rows()
        return compare_database_content(
            expected,
            observed,
            allow_additional_rows=allow_additional_rows,
            ignored_columns_by_row=ignored_columns_by_row,
        )

    def official_carrier_identity(self) -> DatabaseDigests:
        """Require exactly the complete Alpha/Beta reference rows."""
        return compare_database_content(
            {"carriers": official_carrier_rows()},
            {
                "carriers": self._json_rows(
                    "SELECT to_jsonb(row_data)::text FROM carriers AS row_data ORDER BY id"
                )
            },
        )

    def business_rows(self) -> dict[str, list[dict[str, object]]]:
        """Read all physical business rows as typed JSON without retaining them in artifacts."""
        return {
            table: self._json_rows(
                f"SELECT to_jsonb(row_data)::text FROM {table} AS row_data ORDER BY id"
            )
            for table in BUSINESS_TABLES
        }

    def cohort_states(self, shipment_ids: list[str]) -> dict[str, tuple[str, str, str, str]]:
        if not shipment_ids:
            return {}
        literals = ",".join(_sql_literal(item) for item in shipment_ids)
        output = self._psql(
            "SELECT id::text, status::text, "
            "to_char(status_occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), "
            "coalesce(to_char(status_event_received_at AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), ''), "
            "coalesce(status_external_event_id, '') FROM shipments "
            f"WHERE id::text IN ({literals}) ORDER BY id"
        )
        result: dict[str, tuple[str, str, str, str]] = {}
        for line in _output_lines(output):
            parts = line.split("|", maxsplit=4)
            if len(parts) != 5:
                raise ExternalCommandError("psql returned an invalid cohort state row")
            result[parts[0]] = (parts[1], parts[2], parts[3], parts[4])
        return result

    def shipment_event_counts(self, shipment_ids: list[str]) -> dict[str, int]:
        if not shipment_ids:
            return {}
        literals = ",".join(_sql_literal(item) for item in shipment_ids)
        return self.query_pairs(
            "SELECT shipment_id::text, count(*) FROM tracking_events "
            f"WHERE shipment_id::text IN ({literals}) GROUP BY shipment_id ORDER BY shipment_id"
        )

    def event_observations(self, external_event_ids: list[str]) -> dict[str, EventObservation]:
        if not external_event_ids:
            return {}
        literals = ",".join(_sql_literal(item) for item in external_event_ids)
        rows = self._json_rows(
            "SELECT jsonb_build_object("
            "'external_event_id', i.external_event_id, "
            "'inbox_id', i.id::text, "
            "'inbox_status', i.status::text, "
            "'inbox_carrier_id', i.carrier_id::text, "
            "'inbox_received_at', to_char(i.received_at AT TIME ZONE 'UTC', "
            '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\'), '
            "'inbox_processed_at', coalesce(to_char(i.processed_at AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), ''), "
            "'payload_sha256', i.payload_sha256, "
            "'raw_body_hex', encode(i.raw_body, 'hex'), "
            "'parsed_payload', i.parsed_payload, "
            "'tracking_event_id', e.id::text, "
            "'event_received_at', to_char(e.received_at AT TIME ZONE 'UTC', "
            '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\'), '
            "'shipment_id', e.shipment_id::text, "
            "'event_carrier_id', e.carrier_id::text, "
            "'external_status', e.external_status, "
            "'canonical_status', e.canonical_status::text, "
            "'occurred_at', to_char(e.occurred_at AT TIME ZONE 'UTC', "
            '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\'), '
            "'application_result', e.application_result::text, "
            "'previous_shipment_status', e.previous_shipment_status::text, "
            "'resulting_shipment_status', e.resulting_shipment_status::text, "
            "'notification_id', coalesce(min(n.id)::text, ''), "
            "'notification_count', count(n.id), "
            "'matching_notification_count', count(n.id) FILTER (WHERE "
            "n.shipment_id = e.shipment_id AND n.tracking_event_id = e.id))::text "
            "FROM carrier_event_inbox i "
            "LEFT JOIN tracking_events e ON e.inbox_event_id = i.id "
            "LEFT JOIN notifications n ON n.tracking_event_id = e.id "
            f"WHERE i.external_event_id IN ({literals}) "
            "GROUP BY i.id, e.id ORDER BY i.external_event_id"
        )
        result: dict[str, EventObservation] = {}
        for row in rows:
            event_id = row.get("external_event_id")
            raw_hex = row.get("raw_body_hex")
            if not isinstance(event_id, str) or event_id in result or not isinstance(raw_hex, str):
                raise ExternalCommandError("psql returned an invalid benchmark event identity row")
            try:
                raw_hash = hashlib.sha256(bytes.fromhex(raw_hex)).hexdigest()
                result[event_id] = EventObservation(
                    external_event_id=event_id,
                    inbox_id=_required_json_text(row, "inbox_id"),
                    inbox_status=_required_json_text(row, "inbox_status"),
                    inbox_carrier_id=_required_json_text(row, "inbox_carrier_id"),
                    inbox_received_at=_required_json_text(row, "inbox_received_at"),
                    inbox_processed_at=_required_json_text(row, "inbox_processed_at"),
                    payload_sha256=_required_json_text(row, "payload_sha256"),
                    raw_body_sha256=raw_hash,
                    parsed_payload=row.get("parsed_payload"),
                    tracking_event_id=_required_json_text(row, "tracking_event_id"),
                    event_received_at=_required_json_text(row, "event_received_at"),
                    shipment_id=_required_json_text(row, "shipment_id"),
                    event_carrier_id=_required_json_text(row, "event_carrier_id"),
                    external_status=_required_json_text(row, "external_status"),
                    canonical_status=_required_json_text(row, "canonical_status"),
                    occurred_at=_required_json_text(row, "occurred_at"),
                    application_result=_required_json_text(row, "application_result"),
                    previous_shipment_status=_required_json_text(row, "previous_shipment_status"),
                    resulting_shipment_status=_required_json_text(row, "resulting_shipment_status"),
                    notification_id=_required_json_text(row, "notification_id"),
                    notification_count=_required_json_int(row, "notification_count"),
                    matching_notification_count=_required_json_int(
                        row, "matching_notification_count"
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise ExternalCommandError(
                    "psql returned an invalid benchmark event identity row"
                ) from exc
        return result

    def _json_rows(self, sql: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for line in _output_lines(self._psql(sql)):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExternalCommandError("psql returned an invalid logical JSON row") from exc
            if not isinstance(parsed, dict):
                raise ExternalCommandError("psql returned an invalid logical JSON row")
            rows.append(parsed)
        return rows

    def query_pairs(self, sql: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for line in _output_lines(self._psql(sql)):
            parts = line.split("|", maxsplit=1)
            if len(parts) != 2 or parts[0] in result:
                raise ExternalCommandError("psql returned an invalid grouped count row")
            result[parts[0]] = int(parts[1])
        return result

    def query_scalar(self, sql: str) -> str:
        lines = _output_lines(self._psql(sql))
        if len(lines) != 1:
            raise ExternalCommandError("psql scalar probe did not return exactly one row")
        return lines[0]

    def _psql(self, sql: str) -> str:
        command = [
            "docker",
            "exec",
            self.container_id,
            "psql",
            "--no-psqlrc",
            "--username",
            self.user,
            "--dbname",
            self.database,
            "--tuples-only",
            "--no-align",
            "--field-separator=|",
            "--set=ON_ERROR_STOP=1",
            "--command",
            sql,
        ]
        return self.command_runner(command, self.timeout_seconds).stdout


class ResourceSampler:
    """Sample mandatory container resources during one warm-up or measured phase."""

    def __init__(
        self,
        output_path: Path,
        container_ids: Mapping[str, str],
        database: DatabaseProbe,
        interval_seconds: float,
        *,
        command_runner: CommandRunner | None = None,
        command_timeout_seconds: float = 30,
    ) -> None:
        self.output_path = output_path
        self.container_ids = dict(container_ids)
        self.database = database
        self.interval_seconds = interval_seconds
        self.command_runner = command_runner or run_capture
        self.command_timeout_seconds = command_timeout_seconds
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="benchmark-resource-sampler")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(self.command_timeout_seconds * 2, 5))
        if self._thread.is_alive():
            raise ExternalCommandError("resource sampler did not terminate")
        if self.error is not None:
            raise ExternalCommandError(self.error)

    def _run(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.output_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(
                    (
                        "timestamp_utc",
                        "service",
                        "container_id",
                        "cpu_percent",
                        "memory_usage_bytes",
                        "memory_limit_bytes",
                        "postgres_active_connections",
                    )
                )
                next_sample = time.monotonic()
                while True:
                    timestamp = datetime.now(UTC).isoformat()
                    active_connections = self.database.active_connections()
                    for service, identifier in sorted(self.container_ids.items()):
                        stats = self._stats(identifier)
                        writer.writerow(
                            (
                                timestamp,
                                service,
                                identifier,
                                stats[0],
                                stats[1],
                                stats[2],
                                active_connections if service == "postgres" else "",
                            )
                        )
                    stream.flush()
                    next_sample += self.interval_seconds
                    if self._stop.wait(max(0.0, next_sample - time.monotonic())):
                        break
        except Exception:
            self.error = "mandatory resource sampling failed"

    def _stats(self, identifier: str) -> tuple[float, int, int]:
        completed = self.command_runner(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", identifier],
            self.command_timeout_seconds,
        )
        try:
            payload = json.loads(completed.stdout)
            cpu = float(str(payload["CPUPerc"]).rstrip("%"))
            usage, limit = str(payload["MemUsage"]).split("/", maxsplit=1)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ExternalCommandError("docker stats returned an invalid sample") from exc
        return cpu, _parse_memory_bytes(usage.strip()), _parse_memory_bytes(limit.strip())


def validate_resource_samples(path: Path, container_ids: Mapping[str, str]) -> None:
    """Require nonempty, complete sampling cycles; do not invent cadence tolerances."""
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = csv.DictReader(stream)
            timestamp: datetime | None = None
            services: set[str] = set()
            for row in rows:
                instant = datetime.fromisoformat(row["timestamp_utc"])
                if instant.utcoffset() is None:
                    raise ValueError
                if instant != timestamp:
                    if timestamp is not None and (
                        instant <= timestamp or services != set(container_ids)
                    ):
                        raise ValueError
                    timestamp, services = instant, set()
                service = row["service"]
                if service in services or row["container_id"] != container_ids[service]:
                    raise ValueError
                cpu = float(row["cpu_percent"])
                if not math.isfinite(cpu) or cpu < 0:
                    raise ValueError
                if int(row["memory_usage_bytes"]) < 0 or int(row["memory_limit_bytes"]) <= 0:
                    raise ValueError
                connections = row["postgres_active_connections"]
                if service == "postgres":
                    if int(connections) < 0:
                        raise ValueError
                elif connections != "":
                    raise ValueError
                services.add(service)
            if timestamp is None or services != set(container_ids):
                raise ValueError
    except (OSError, KeyError, TypeError, ValueError, csv.Error) as exc:
        raise ExternalCommandError(
            "resource CSV is missing or has incomplete/invalid samples"
        ) from exc


def run_capture(command: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    """Run one bounded argv command without shell or sensitive failure rendering."""
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExternalCommandError(
            f"external command could not complete: {_safe_command_name(command)}"
        ) from exc
    if completed.returncode != 0:
        raise ExternalCommandError(f"external command failed: {_safe_command_name(command)}")
    return completed


def write_database_counts(path: Path, snapshots: list[DatabaseSnapshot]) -> None:
    """Write absolute snapshots plus explicit deltas in one auditable CSV."""
    if len(snapshots) < 2:
        raise ValueError("database count artifact requires at least two snapshots")
    rows: list[tuple[str, str, int]] = []
    for snapshot in snapshots:
        rows.extend((snapshot.label, metric, value) for metric, value in snapshot.metrics().items())
    for before, after in pairwise(snapshots):
        all_metrics = set(before.metrics()) | set(after.metrics())
        label = f"delta:{before.label}->{after.label}"
        rows.extend(
            (label, metric, after.metrics().get(metric, 0) - before.metrics().get(metric, 0))
            for metric in sorted(all_metrics)
        )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("snapshot", "metric", "value"))
        writer.writerows(rows)


def _add_check(
    report: dict[str, dict[str, object]],
    name: str,
    expected: object,
    observed: object,
    *,
    matches: bool | None = None,
) -> None:
    report[name] = {
        "expected": expected,
        "observed": observed,
        "matches": expected == observed if matches is None else matches,
    }


def _environment_map(inspection: Mapping[str, Any]) -> dict[str, str]:
    values = inspection.get("Config", {}).get("Env", []) or []
    result: dict[str, str] = {}
    for value in values:
        if isinstance(value, str) and "=" in value:
            key, item = value.split("=", maxsplit=1)
            result[key] = item
    return result


def _required_nonsecret_environment(environment: Mapping[str, str], key: str) -> str:
    value = environment.get(key)
    if not value:
        raise ExternalCommandError(f"PostgreSQL container does not expose required {key}")
    return value


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _option_value(command: list[str], option: str) -> str | None:
    try:
        return command[command.index(option) + 1]
    except (ValueError, IndexError):
        return None


def _parse_memory_bytes(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b?|[kmgt])?\s*", value.casefold())
    if match is None:
        raise ValueError(f"unsupported memory limit syntax: {value!r}")
    amount = float(match.group(1))
    suffix = (match.group(2) or "b").rstrip("b")
    powers = {"": 0, "k": 1, "ki": 1, "m": 2, "mi": 2, "g": 3, "gi": 3, "t": 4, "ti": 4}
    base = 1024 if "i" in suffix or suffix in {"k", "m", "g", "t"} else 1000
    return int(amount * (base ** powers[suffix]))


def _add_numeric_check(
    report: dict[str, dict[str, object]],
    name: str,
    expected: object,
    observed: object,
) -> None:
    expected_normalized = _normalize_finite_number(expected)
    observed_normalized = _normalize_finite_number(observed)
    report[name] = {
        "expected": expected,
        "observed": observed,
        "expected_normalized": expected_normalized,
        "observed_normalized": observed_normalized,
        "matches": (
            expected_normalized is not None
            and observed_normalized is not None
            and expected_normalized == observed_normalized
        ),
    }


def _normalize_finite_number(value: object) -> str | None:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (str, int, float, Decimal))
    ):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    if parsed == 0:
        return "0"
    return format(parsed.normalize(), "f")


def _sql_literal(value: str) -> str:
    if not value.isascii() or len(value) > 128:
        raise ValueError("unsafe benchmark identity for psql probe")
    return "'" + value.replace("'", "''") + "'"


def _output_lines(output: str) -> list[str]:
    return [line for line in (item.strip() for item in output.splitlines()) if line]


def _required_json_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"logical JSON row lacks {key}")
    return value


def _required_json_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"logical JSON row lacks {key}")
    return value


def _safe_command_name(command: list[str]) -> str:
    if not command:
        return "external tool"
    return " ".join(command[:2])
