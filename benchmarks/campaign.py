"""Validated campaign protocol and frozen dataset cohort resolution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, model_validator

from benchmarks.artifact import DATASET_SCHEMA_VERSION, load_authenticated_document
from benchmarks.database_contract import STRUCTURAL_SCHEMA_CONTRACT_VERSION
from benchmarks.semantic import validate_semantic_document

MAX_USERS_PER_PHASE = 188
WARMUP_SECONDS = 60
MEASUREMENT_SECONDS = 300
OFFICIAL_REPETITIONS = 5
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_RAW_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class StrictModel(BaseModel):
    """Reject undeclared protocol fields instead of silently ignoring drift."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RequestWeights(StrictModel):
    """Per-request mixed workload contract."""

    shipment_detail: int = Field(default=25, ge=0)
    timeline: int = Field(default=25, ge=0)
    webhook: int = Field(default=30, ge=0)
    shipment_list: int = Field(default=20, ge=0)

    @model_validator(mode="after")
    def validate_percentage_contract(self) -> Self:
        if sum(self.model_dump().values()) != 100:
            raise ValueError("request weights must sum to exactly 100 percent")
        return self


class LoadLevel(StrictModel):
    """Host-specific concurrency parameters frozen by an official manifest."""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    users: PositiveInt
    spawn_rate: PositiveFloat

    @model_validator(mode="after")
    def validate_capacity(self) -> Self:
        if self.users > MAX_USERS_PER_PHASE:
            raise ValueError(f"users cannot exceed {MAX_USERS_PER_PHASE}")
        if self.users % 4 != 0:
            raise ValueError(
                "users must be a multiple of four to balance carrier and initial state"
            )
        return self


class ResourceLimit(StrictModel):
    """Explicit CPU and memory limits for one benchmark service."""

    cpus: str = Field(min_length=1, max_length=32)
    memory: str = Field(min_length=1, max_length=32)


class ResourceLimits(StrictModel):
    app: ResourceLimit
    postgres: ResourceLimit
    loadgen: ResourceLimit


class PoolParameters(StrictModel):
    size: PositiveInt
    max_overflow: int = Field(ge=0)
    timeout_seconds: PositiveFloat
    statement_timeout_ms: PositiveInt


class ImageDigests(StrictModel):
    app: str
    postgres: str
    loadgen: str

    @model_validator(mode="after")
    def validate_sha256_digests(self) -> Self:
        invalid = [
            name
            for name, digest in self.model_dump().items()
            if not _DIGEST_PATTERN.fullmatch(digest)
        ]
        if invalid:
            raise ValueError(f"image digests must be sha256 values: {', '.join(invalid)}")
        return self


class CohortReferences(StrictModel):
    dataset_manifest: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warmup: Literal["mutable_component.warmup"] = "mutable_component.warmup"
    measurement: Literal["mutable_component.measurement"] = "mutable_component.measurement"
    timeline: Literal["timeline_read_cohort"] = "timeline_read_cohort"


class TelemetryContract(StrictModel):
    log_level: str = Field(min_length=1)
    log_format: Literal["json", "console"]
    tracing_enabled: bool
    tracing_sampling: float = Field(ge=0.0, le=1.0)


class ComposeServices(StrictModel):
    app: Literal["app"] = "app"
    postgres: Literal["db"] = "db"
    loadgen: Literal["loadgen"] = "loadgen"


class EnvironmentContract(StrictModel):
    compose_project: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    compose_file: str = Field(min_length=1)
    services: ComposeServices


class DatabaseContract(StrictModel):
    """Release-specific observed PostgreSQL structure required before warm-up."""

    structural_contract_version: int
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alembic_heads: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_release_schema_contract(self) -> Self:
        if self.structural_contract_version != STRUCTURAL_SCHEMA_CONTRACT_VERSION:
            raise ValueError("database structural contract version is not supported")
        if len(set(self.alembic_heads)) != len(self.alembic_heads) or any(
            not re.fullmatch(r"[a-zA-Z0-9_]+", head) for head in self.alembic_heads
        ):
            raise ValueError("database Alembic heads must be unique stable identifiers")
        return self


class TimeoutContract(StrictModel):
    model_config = ConfigDict(allow_inf_nan=False)

    preparation_seconds: PositiveFloat
    phase_start_seconds: PositiveFloat
    warmup_process_seconds: PositiveFloat = Field(gt=WARMUP_SECONDS)
    measurement_process_seconds: PositiveFloat = Field(gt=MEASUREMENT_SECONDS)
    drain_seconds: PositiveFloat
    request_seconds: PositiveFloat
    command_seconds: PositiveFloat

    @model_validator(mode="after")
    def validate_drain_budget(self) -> Self:
        if not self.warmup_process_seconds > WARMUP_SECONDS + self.drain_seconds:
            raise ValueError("warmup_process_seconds must exceed 60 + drain_seconds")
        if not self.measurement_process_seconds > MEASUREMENT_SECONDS + self.drain_seconds:
            raise ValueError("measurement_process_seconds must exceed 300 + drain_seconds")
        return self


class HostIdentity(StrictModel):
    """Allowlisted expectations; every field is essential for an official campaign."""

    os: Literal["Windows"] | None = None
    os_version: str | None = Field(default=None, pattern=r"^\d+(\.\d+)+$")
    os_build: str | None = Field(default=None, pattern=r"^\d+\.\d+$")
    cpu_model: str | None = Field(default=None, min_length=1, max_length=160)
    physical_cores: PositiveInt | None = None
    logical_processors: PositiveInt | None = None
    physical_memory_bytes: PositiveInt | None = None
    docker_engine: str | None = Field(default=None, pattern=r"^\d+[\w.+-]*$")
    docker_compose: str | None = Field(default=None, pattern=r"^\d+[\w.+-]*$")
    wsl_version: str | None = Field(default=None, pattern=r"^\d+(\.\d+)+$")
    wsl_kernel: str | None = Field(default=None, pattern=r"^[\w.+-]+WSL2[\w.+-]*$")
    docker_cpus: PositiveInt | None = None
    docker_memory_bytes: PositiveInt | None = None


class HostConditions(StrictModel):
    """Exact dynamic expectations, not uncalibrated utilization thresholds."""

    ac_power: bool | None = None
    power_plan_guid: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"
    )
    concurrent_containers: int | None = Field(default=None, ge=0)


class HostContract(StrictModel):
    identity: HostIdentity
    conditions: HostConditions
    docker_memory_tolerance_bytes: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_docker_memory_tolerance(self) -> Self:
        if (
            self.docker_memory_tolerance_bytes is not None
            and self.identity.docker_memory_bytes is None
        ):
            raise ValueError("docker memory tolerance requires an expected docker_memory_bytes")
        return self


class CampaignManifest(StrictModel):
    """Complete protocol; host-specific values are mandatory only in an actual file."""

    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    official: bool
    release: Literal["v1.0.0"]
    git_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    profile: Literal["timeline", "ingestion", "mixed"]
    loads: tuple[LoadLevel, ...] = Field(min_length=1)
    workers: Literal[1]
    warmup_seconds: Literal[60]
    measurement_seconds: Literal[300]
    stabilization_seconds: float = Field(ge=0, allow_inf_nan=False)
    repetitions: PositiveInt
    warmup_quota_per_shipment: PositiveInt
    occurred_at_step_microseconds: PositiveInt
    warmup_occurred_at_base: datetime
    measurement_occurred_at_base: datetime
    weights: RequestWeights
    resources: ResourceLimits
    pool: PoolParameters
    images: ImageDigests
    cohorts: CohortReferences
    telemetry: TelemetryContract
    environment: EnvironmentContract
    host: HostContract
    database: DatabaseContract
    timeouts: TimeoutContract
    collection_interval_seconds: PositiveFloat

    @model_validator(mode="after")
    def validate_protocol(self) -> Self:
        if self.official and self.repetitions != OFFICIAL_REPETITIONS:
            raise ValueError("an official campaign must contain exactly five repetitions")
        if self.official and self.stabilization_seconds <= 0:
            raise ValueError("official stabilization_seconds must be positive")
        if self.official and any(
            value is None
            for section in (self.host.identity, self.host.conditions)
            for value in section.model_dump().values()
        ):
            raise ValueError("official campaign requires every essential host expectation")
        if self.warmup_quota_per_shipment % 2 != 0:
            raise ValueError("warm-up quota q must be even for every Shipment")
        if self.warmup_occurred_at_base.tzinfo is None:
            raise ValueError("warm-up occurred_at base must be timezone-aware")
        if self.measurement_occurred_at_base.tzinfo is None:
            raise ValueError("measurement occurred_at base must be timezone-aware")
        if len({load.name for load in self.loads}) != len(self.loads):
            raise ValueError("load level names must be unique")
        expected_weights = {
            "mixed": {
                "shipment_detail": 25,
                "timeline": 25,
                "webhook": 30,
                "shipment_list": 20,
            },
            "timeline": {
                "shipment_detail": 50,
                "timeline": 50,
                "webhook": 0,
                "shipment_list": 0,
            },
            "ingestion": {
                "shipment_detail": 0,
                "timeline": 0,
                "webhook": 100,
                "shipment_list": 0,
            },
        }[self.profile]
        if self.weights.model_dump() != expected_weights:
            raise ValueError(
                f"request weights for profile {self.profile} must remain exactly {expected_weights}"
            )
        return self


class CohortSlot(StrictModel):
    slot_id: str = Field(min_length=1)
    shipment_id: UUID
    carrier_code: Literal["carrier-alpha", "carrier-beta"]
    tracking_code: str = Field(min_length=1, max_length=80)
    initial_status: Literal["IN_TRANSIT", "OUT_FOR_DELIVERY"]
    initial_occurred_at: datetime


class TimelineSlot(StrictModel):
    shipment_id: UUID
    carrier_code: Literal["carrier-alpha", "carrier-beta"]
    tracking_code: str = Field(min_length=1, max_length=80)
    initial_status: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class CampaignBundle:
    manifest: CampaignManifest
    warmup: tuple[CohortSlot, ...]
    measurement: tuple[CohortSlot, ...]
    timeline: tuple[TimelineSlot, ...]
    dataset_path: Path
    dataset_sha256: str
    dataset_document: dict[str, object]


def load_campaign(path: Path) -> CampaignBundle:
    """Load one campaign and cross-check its referenced frozen dataset cohorts."""
    manifest = CampaignManifest.model_validate_json(path.read_text(encoding="utf-8"))
    dataset_path = Path(manifest.cohorts.dataset_manifest)
    if not dataset_path.is_absolute():
        dataset_path = (path.parent / dataset_path).resolve()
    dataset, authenticated_digest = load_authenticated_document(
        dataset_path,
        expected_sha256=manifest.cohorts.dataset_sha256,
    )
    validate_semantic_document(dataset)
    metadata = dataset.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise ValueError("campaign dataset schema/version is not supported")
    cohorts = dataset.get("cohorts")
    if not isinstance(cohorts, dict):
        raise ValueError("dataset artifact has no cohorts object")
    mutable = cohorts.get("mutable_component")
    if not isinstance(mutable, dict):
        raise ValueError("dataset manifest has no mutable_component")
    warmup = tuple(CohortSlot.model_validate(item) for item in mutable.get("warmup", []))
    measurement = tuple(CohortSlot.model_validate(item) for item in mutable.get("measurement", []))
    timeline = tuple(
        TimelineSlot.model_validate(item) for item in cohorts.get("timeline_read_cohort", [])
    )
    _validate_cohorts(manifest, warmup, measurement, timeline)
    _validate_cohort_rows_against_tables(dataset, warmup, measurement, timeline)
    return CampaignBundle(
        manifest,
        warmup,
        measurement,
        timeline,
        dataset_path,
        authenticated_digest,
        dataset,
    )


def _validate_cohorts(
    manifest: CampaignManifest,
    warmup: tuple[CohortSlot, ...],
    measurement: tuple[CohortSlot, ...],
    timeline: tuple[TimelineSlot, ...],
) -> None:
    if len(warmup) != MAX_USERS_PER_PHASE or len(measurement) != MAX_USERS_PER_PHASE:
        raise ValueError("frozen mutable cohorts must each contain exactly 188 Shipments")
    warm_ids = {item.shipment_id for item in warmup}
    measure_ids = {item.shipment_id for item in measurement}
    timeline_ids = {item.shipment_id for item in timeline}
    if len(warm_ids) != len(warmup) or len(measure_ids) != len(measurement):
        raise ValueError("mutable cohort contains duplicate Shipment ownership")
    if warm_ids & measure_ids:
        raise ValueError("warm-up and measurement cohorts must be disjoint")
    if timeline_ids & (warm_ids | measure_ids):
        raise ValueError("timeline cohort must exclude every mutable Shipment")
    if len(timeline_ids) != len(timeline):
        raise ValueError("timeline cohort contains duplicate Shipment identities")
    all_mutable = (*warmup, *measurement)
    slot_ids = [item.slot_id for item in all_mutable]
    normalized_slots = [re.sub(r"[^a-z0-9-]", "-", item.casefold()) for item in slot_ids]
    if len(set(slot_ids)) != len(slot_ids) or len(set(normalized_slots)) != len(normalized_slots):
        raise ValueError("mutable cohort contains a colliding event slot identity")
    mutable_tracking = [item.tracking_code for item in all_mutable]
    if len(set(mutable_tracking)) != len(mutable_tracking):
        raise ValueError("mutable cohort contains a duplicate tracking code")
    timeline_tracking = [item.tracking_code for item in timeline]
    if len(set(timeline_tracking)) != len(timeline_tracking):
        raise ValueError("timeline cohort contains a duplicate tracking code")
    for name, cohort in (("warm-up", warmup), ("measurement", measurement)):
        distribution: dict[tuple[str, str], int] = {}
        for item in cohort:
            key = (item.carrier_code, item.initial_status)
            distribution[key] = distribution.get(key, 0) + 1
        expected = {
            (carrier, status): 47
            for carrier in ("carrier-alpha", "carrier-beta")
            for status in ("IN_TRANSIT", "OUT_FOR_DELIVERY")
        }
        if distribution != expected:
            raise ValueError(f"{name} cohort is not balanced by carrier and initial status")
    if not timeline:
        raise ValueError("timeline cohort cannot be empty")
    if max(load.users for load in manifest.loads) > min(len(warmup), len(measurement)):
        raise ValueError("load users exceed exclusive mutable cohort capacity")
    if manifest.warmup_occurred_at_base <= max(item.initial_occurred_at for item in warmup):
        raise ValueError("warm-up occurred_at base must follow every seeded cohort key")
    if manifest.measurement_occurred_at_base <= max(
        item.initial_occurred_at for item in measurement
    ):
        raise ValueError("measurement occurred_at base must follow every seeded cohort key")


def _validate_cohort_rows_against_tables(
    document: dict[str, object],
    warmup: tuple[CohortSlot, ...],
    measurement: tuple[CohortSlot, ...],
    timeline: tuple[TimelineSlot, ...],
) -> None:
    tables = document.get("tables")
    if not isinstance(tables, dict) or not isinstance(tables.get("shipments"), list):
        raise ValueError("authenticated dataset has no Shipment table")
    shipments = {
        str(item.get("id")): item for item in tables["shipments"] if isinstance(item, dict)
    }
    for slot in (*warmup, *measurement):
        row = shipments.get(str(slot.shipment_id))
        if row is None:
            raise ValueError("mutable cohort references an unknown authenticated Shipment")
        expected = (
            slot.carrier_code,
            slot.tracking_code,
            slot.initial_status,
            slot.initial_occurred_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )
        observed = (
            row.get("carrier_code"),
            row.get("tracking_code"),
            row.get("status"),
            row.get("status_occurred_at"),
        )
        if observed != expected:
            raise ValueError("mutable cohort diverges from its authenticated Shipment row")
    for timeline_slot in timeline:
        row = shipments.get(str(timeline_slot.shipment_id))
        if row is None or (
            row.get("carrier_code"),
            row.get("tracking_code"),
            row.get("status"),
        ) != (
            timeline_slot.carrier_code,
            timeline_slot.tracking_code,
            timeline_slot.initial_status,
        ):
            raise ValueError("timeline cohort diverges from its authenticated Shipment row")


def balanced_active_slots(cohort: tuple[CohortSlot, ...], users: int) -> tuple[CohortSlot, ...]:
    """Select a deterministic four-stratum-balanced active prefix."""
    keys = (
        ("carrier-alpha", "IN_TRANSIT"),
        ("carrier-alpha", "OUT_FOR_DELIVERY"),
        ("carrier-beta", "IN_TRANSIT"),
        ("carrier-beta", "OUT_FOR_DELIVERY"),
    )
    per_stratum = users // len(keys)
    grouped = {
        key: sorted(
            (slot for slot in cohort if (slot.carrier_code, slot.initial_status) == key),
            key=lambda slot: slot.slot_id,
        )[:per_stratum]
        for key in keys
    }
    if any(len(slots) != per_stratum for slots in grouped.values()):
        raise ValueError("frozen cohort cannot satisfy the requested balanced load")
    return tuple(grouped[key][position] for position in range(per_stratum) for key in keys)


def deterministic_event_id(
    profile: str,
    load_name: str,
    phase: str,
    slot_id: str,
    sequence: int,
) -> str:
    """Build the campaign identity shared by loadgen and external verification."""
    normalized_slot = re.sub(r"[^a-z0-9-]", "-", slot_id.casefold())
    value = f"ff-{profile}-{load_name}-{phase}-{normalized_slot}-e{sequence:06d}"
    if len(value) > 128 or not value.isascii():
        raise ValueError("deterministic external event ID exceeds the public contract")
    return value


def cycle_target_status(initial_status: str, sequence: int) -> str:
    """Resolve one edge of the frozen sustainable two-state mutation cycle."""
    if initial_status not in {"IN_TRANSIT", "OUT_FOR_DELIVERY"}:
        raise ValueError("mutable slot must start inside the approved two-state cycle")
    if sequence <= 0:
        raise ValueError("event sequence must be positive")
    if sequence % 2 == 1:
        return "OUT_FOR_DELIVERY" if initial_status == "IN_TRANSIT" else "IN_TRANSIT"
    return initial_status


def carrier_payload(
    slot: CohortSlot,
    event_id: str,
    canonical_status: str,
    occurred_at: datetime,
) -> dict[str, object]:
    """Build the one frozen logical payload consumed by both runner and loadgen."""
    if occurred_at.utcoffset() is None:
        raise ValueError("benchmark occurred_at must be timezone-aware")
    if canonical_status not in {"IN_TRANSIT", "OUT_FOR_DELIVERY"}:
        raise ValueError("benchmark payload target must stay inside the approved cycle")
    instant_text = (
        occurred_at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    if slot.carrier_code == "carrier-alpha":
        status = "MOVING" if canonical_status == "IN_TRANSIT" else "OUT_FOR_DELIVERY"
        return {
            "eventId": event_id,
            "trackingCode": slot.tracking_code,
            "status": status,
            "eventDate": instant_text,
            "city": "Synthetic City",
            "description": "Deterministic benchmark event",
        }
    event_type = "hub_scan" if canonical_status == "IN_TRANSIT" else "courier_route"
    return {
        "id": event_id,
        "tracking_number": slot.tracking_code,
        "event": {
            "type": event_type,
            "occurred_at": instant_text,
            "details": "Deterministic benchmark event",
        },
        "location": {"city": "Synthetic City", "state": "SP"},
    }


def canonical_payload_bytes(payload: object) -> bytes:
    """Serialize exactly once using the frozen outbound HTTP byte contract."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
