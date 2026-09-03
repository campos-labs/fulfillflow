"""Pure deterministic logical datasets shared by seeds and benchmarks."""

from __future__ import annotations

import base64
import hashlib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from benchmarks.artifact import (
    BENCHMARK_HASH_NAME,
    BENCHMARK_MANIFEST_NAME,
    DATASET_SCHEMA_VERSION,
    canonical_json_bytes,
    load_authenticated_document,
)
from fulfillflow.carriers.public import normalize_carrier_event
from fulfillflow.notifications.public import build_notification
from fulfillflow.shipments.public import (
    Shipment,
    ShipmentApplicationResult,
    ShipmentStatus,
)

DATASET_SEED = 20260828
BASE_INSTANT = datetime(2026, 8, 28, tzinfo=UTC)

DatasetName = Literal["demo", "benchmark"]

_ALPHA_ID = UUID("00000000-0000-4000-8000-000000000100")
_BETA_ID = UUID("00000000-0000-4000-8000-000000000101")
_CARRIERS = (
    (_ALPHA_ID, "carrier-alpha", "alpha"),
    (_BETA_ID, "carrier-beta", "beta"),
)

_STATUS_DISTRIBUTIONS: dict[DatasetName, tuple[tuple[ShipmentStatus, int], ...]] = {
    "demo": (
        (ShipmentStatus.PENDING, 7),
        (ShipmentStatus.POSTED, 7),
        (ShipmentStatus.IN_TRANSIT, 6),
        (ShipmentStatus.OUT_FOR_DELIVERY, 6),
        (ShipmentStatus.DELIVERED, 6),
        (ShipmentStatus.EXCEPTION, 6),
        (ShipmentStatus.RETURNED, 6),
        (ShipmentStatus.CANCELLED, 6),
    ),
    "benchmark": (
        (ShipmentStatus.PENDING, 188),
        (ShipmentStatus.POSTED, 188),
        (ShipmentStatus.IN_TRANSIT, 188),
        (ShipmentStatus.OUT_FOR_DELIVERY, 188),
        (ShipmentStatus.DELIVERED, 187),
        (ShipmentStatus.EXCEPTION, 187),
        (ShipmentStatus.RETURNED, 187),
        (ShipmentStatus.CANCELLED, 187),
    ),
}

_RESULT_DISTRIBUTIONS: dict[DatasetName, dict[ShipmentApplicationResult, int]] = {
    "demo": {
        ShipmentApplicationResult.APPLIED: 86,
        ShipmentApplicationResult.NO_STATE_CHANGE: 1,
        ShipmentApplicationResult.IGNORED_STALE: 1,
        ShipmentApplicationResult.IGNORED_INVALID_TRANSITION: 1,
    },
    "benchmark": {
        ShipmentApplicationResult.APPLIED: 2_998,
        ShipmentApplicationResult.NO_STATE_CHANGE: 3_002,
        ShipmentApplicationResult.IGNORED_STALE: 2_814,
        ShipmentApplicationResult.IGNORED_INVALID_TRANSITION: 6_186,
    },
}

_BENCHMARK_APPLIED_PATHS: dict[ShipmentStatus, tuple[ShipmentStatus, ...]] = {
    ShipmentStatus.PENDING: (),
    ShipmentStatus.POSTED: (ShipmentStatus.POSTED,),
    ShipmentStatus.IN_TRANSIT: (ShipmentStatus.POSTED, ShipmentStatus.IN_TRANSIT),
    ShipmentStatus.OUT_FOR_DELIVERY: (
        ShipmentStatus.POSTED,
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
    ),
    ShipmentStatus.DELIVERED: (
        ShipmentStatus.POSTED,
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.DELIVERED,
    ),
    ShipmentStatus.EXCEPTION: (
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.EXCEPTION,
    ),
    ShipmentStatus.RETURNED: (
        ShipmentStatus.POSTED,
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.RETURNED,
    ),
    ShipmentStatus.CANCELLED: (),
}

_EXTERNAL_STATUS: dict[str, dict[ShipmentStatus, str]] = {
    "alpha": {
        ShipmentStatus.POSTED: "CREATED",
        ShipmentStatus.IN_TRANSIT: "MOVING",
        ShipmentStatus.OUT_FOR_DELIVERY: "OUT_FOR_DELIVERY",
        ShipmentStatus.DELIVERED: "DELIVERED",
        ShipmentStatus.EXCEPTION: "PROBLEM",
        ShipmentStatus.RETURNED: "RETURNED",
    },
    "beta": {
        ShipmentStatus.POSTED: "label_created",
        ShipmentStatus.IN_TRANSIT: "hub_scan",
        ShipmentStatus.OUT_FOR_DELIVERY: "courier_route",
        ShipmentStatus.DELIVERED: "completed",
        ShipmentStatus.EXCEPTION: "delivery_issue",
        ShipmentStatus.RETURNED: "returned_origin",
    },
}


@dataclass(frozen=True, slots=True)
class OrderRow:
    id: UUID
    external_reference: str
    recipient_name: str
    recipient_email: str
    recipient_postal_code: str
    recipient_city: str
    recipient_state: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ShipmentRow:
    id: UUID
    order_id: UUID
    carrier_id: UUID
    carrier_code: str
    tracking_code: str
    status: str
    status_occurred_at: datetime
    status_event_received_at: datetime | None
    status_external_event_id: str | None
    estimated_delivery_date: date | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InboxRow:
    id: UUID
    carrier_id: UUID
    external_event_id: str
    payload_sha256: str
    raw_body: bytes
    parsed_payload: dict[str, object] | None
    received_at: datetime
    status: str
    error_code: str | None
    error_detail: str | None
    processed_at: datetime | None
    request_id: UUID


@dataclass(frozen=True, slots=True)
class TrackingEventRow:
    id: UUID
    inbox_event_id: UUID
    shipment_id: UUID
    carrier_id: UUID
    external_status: str
    canonical_status: str
    description: str | None
    location: str | None
    occurred_at: datetime
    received_at: datetime
    application_result: str
    previous_shipment_status: str | None
    resulting_shipment_status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationRow:
    id: UUID
    shipment_id: UUID
    tracking_event_id: UUID
    channel: str
    recipient: str
    template_key: str
    message: str
    status: str
    error_detail: str | None
    created_at: datetime
    simulated_at: datetime | None


type DatasetRow = OrderRow | ShipmentRow | InboxRow | TrackingEventRow | NotificationRow


@dataclass(frozen=True, slots=True)
class LogicalDataset:
    name: DatasetName
    seed: int
    base_instant: datetime
    orders: tuple[OrderRow, ...]
    shipments: tuple[ShipmentRow, ...]
    inboxes: tuple[InboxRow, ...]
    tracking_events: tuple[TrackingEventRow, ...]
    notifications: tuple[NotificationRow, ...]

    @property
    def counts(self) -> dict[str, int]:
        """Return table cardinalities derived from the generated rows."""
        return {
            "orders": len(self.orders),
            "shipments": len(self.shipments),
            "carrier_event_inbox": len(self.inboxes),
            "tracking_events": len(self.tracking_events),
            "notifications": len(self.notifications),
        }

    @property
    def status_counts(self) -> dict[str, int]:
        """Return the actual Shipment matrix without a duplicate total constant."""
        return dict(sorted(Counter(item.status for item in self.shipments).items()))

    @property
    def carrier_counts(self) -> dict[str, int]:
        """Return the actual carrier distribution."""
        return dict(sorted(Counter(item.carrier_code for item in self.shipments).items()))

    @property
    def tracking_result_counts(self) -> dict[str, int]:
        """Return application outcomes derived from canonical state-machine results."""
        return dict(
            sorted(Counter(item.application_result for item in self.tracking_events).items())
        )

    def canonical_bytes(self) -> bytes:
        """Serialize the full logical dataset using canonical UTF-8 JSON."""
        return _canonical_json_bytes(self.logical_document())

    def logical_hash(self) -> str:
        """Hash the canonical dataset representation, which has no hash field."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def logical_document(self) -> dict[str, object]:
        """Return the complete, self-hash-free and order-independent artifact."""
        cohorts: dict[str, object] = {}
        if self.name == "benchmark":
            cohorts = benchmark_cohorts(self)
        return {
            "metadata": {
                "schema_version": DATASET_SCHEMA_VERSION,
                "dataset": self.name,
                "seed": self.seed,
                "base_instant": _iso_datetime(self.base_instant),
                "counts": self.counts,
                "shipment_statuses": self.status_counts,
                "shipment_carriers": self.carrier_counts,
                "tracking_results": self.tracking_result_counts,
                "canonicalization": (
                    "UTF-8 JSON; sorted object keys; compact separators; no NaN; every table "
                    "and cohort sorted by its explicit canonical identity; no trailing newline"
                ),
            },
            "cohorts": cohorts,
            "tables": {
                "orders": _canonical_rows(self.orders),
                "shipments": _canonical_rows(self.shipments),
                "carrier_event_inbox": _canonical_rows(self.inboxes),
                "tracking_events": _canonical_rows(self.tracking_events),
                "notifications": _canonical_rows(self.notifications),
            },
        }

    def database_rows(self) -> dict[str, list[dict[str, object]]]:
        """Project logical records to reflected database table columns."""
        return {
            "orders": [_database_row(item) for item in self.orders],
            "shipments": [
                _database_row(item, excluded=frozenset({"carrier_code"})) for item in self.shipments
            ],
            "carrier_event_inbox": [_database_row(item) for item in self.inboxes],
            "tracking_events": [_database_row(item) for item in self.tracking_events],
            "notifications": [_database_row(item) for item in self.notifications],
        }


def deterministic_uuid(logical_key: str, *, seed: int = DATASET_SEED) -> UUID:
    """Derive an RFC 4122 variant UUID with explicit version-4 bits."""
    digest = bytearray(hashlib.sha256(f"{seed}:{logical_key}".encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    identifier = UUID(bytes=bytes(digest))
    if identifier.version != 4 or identifier.variant != "specified in RFC 4122":
        raise AssertionError("deterministic UUID did not retain RFC 4122 version-4 bits")
    return identifier


def generate_dataset(name: DatasetName) -> LogicalDataset:
    """Generate and validate one immutable logical dataset."""
    statuses = _expanded_statuses(name)
    expected_shipments = 50 if name == "demo" else 1_500
    expected_orders = 25 if name == "demo" else 1_000
    if len(statuses) != expected_shipments:
        raise AssertionError("Shipment distribution does not match the dataset size")

    shipments: list[ShipmentRow] = []
    inboxes: list[InboxRow] = []
    events: list[TrackingEventRow] = []
    notifications: list[NotificationRow] = []
    statuses_by_order: dict[int, list[ShipmentStatus]] = defaultdict(list)
    remaining_results = Counter(_RESULT_DISTRIBUTIONS[name])

    for shipment_index, final_status in enumerate(statuses, start=1):
        order_index = _order_index(name, shipment_index)
        statuses_by_order[order_index].append(final_status)
        carrier_id, carrier_code, adapter_key = _CARRIERS[(shipment_index - 1) % 2]
        shipment_id = deterministic_uuid(f"{name}:shipment:{shipment_index}")
        order_id = deterministic_uuid(f"{name}:order:{order_index}")
        created_at = BASE_INSTANT + timedelta(days=1, seconds=shipment_index)
        domain_shipment = Shipment(
            id=shipment_id,
            order_id=order_id,
            carrier_id=carrier_id,
            tracking_code=_tracking_code(name, carrier_code, shipment_index),
            status=ShipmentStatus.PENDING,
            status_occurred_at=created_at,
            status_event_received_at=None,
            status_external_event_id=None,
            estimated_delivery_date=(BASE_INSTANT + timedelta(days=7)).date(),
            shipped_at=None,
            delivered_at=None,
            created_at=created_at,
            updated_at=created_at,
        )
        if final_status is ShipmentStatus.CANCELLED:
            domain_shipment.cancel(created_at + timedelta(seconds=1))

        path = _applied_path(name, final_status, shipment_index)
        event_sequence = 0
        for target in path:
            event_sequence += 1
            _append_event(
                name=name,
                shipment_index=shipment_index,
                event_sequence=event_sequence,
                target=target,
                expected_result=ShipmentApplicationResult.APPLIED,
                domain_shipment=domain_shipment,
                carrier_id=carrier_id,
                carrier_code=carrier_code,
                adapter_key=adapter_key,
                recipient=_recipient_email(name, order_index),
                inboxes=inboxes,
                events=events,
                notifications=notifications,
            )
            remaining_results[ShipmentApplicationResult.APPLIED] -= 1

        extra_count = _extra_tracking_count(name, shipment_index, len(path))
        for _ in range(extra_count):
            event_sequence += 1
            expected_result = _extra_result(
                name,
                shipment_index,
                final_status,
                remaining_results,
            )
            target = _target_for_result(expected_result, domain_shipment.status)
            _append_event(
                name=name,
                shipment_index=shipment_index,
                event_sequence=event_sequence,
                target=target,
                expected_result=expected_result,
                domain_shipment=domain_shipment,
                carrier_id=carrier_id,
                carrier_code=carrier_code,
                adapter_key=adapter_key,
                recipient=_recipient_email(name, order_index),
                inboxes=inboxes,
                events=events,
                notifications=notifications,
            )
            remaining_results[expected_result] -= 1

        if domain_shipment.status is not final_status:
            raise AssertionError(
                f"timeline for {name} Shipment {shipment_index} ended in "
                f"{domain_shipment.status}, expected {final_status}"
            )
        shipments.append(
            ShipmentRow(
                id=domain_shipment.id,
                order_id=domain_shipment.order_id,
                carrier_id=domain_shipment.carrier_id,
                carrier_code=carrier_code,
                tracking_code=domain_shipment.tracking_code,
                status=domain_shipment.status.value,
                status_occurred_at=domain_shipment.status_occurred_at,
                status_event_received_at=domain_shipment.status_event_received_at,
                status_external_event_id=domain_shipment.status_external_event_id,
                estimated_delivery_date=domain_shipment.estimated_delivery_date,
                shipped_at=domain_shipment.shipped_at,
                delivered_at=domain_shipment.delivered_at,
                created_at=domain_shipment.created_at,
                updated_at=domain_shipment.updated_at,
            )
        )

    if any(remaining_results.values()):
        raise AssertionError(
            f"{name} application-result allocation diverged: {dict(remaining_results)}"
        )

    if name == "demo":
        _append_demo_operational_inboxes(inboxes)

    orders = tuple(
        _order_row(name, order_index, statuses_by_order[order_index])
        for order_index in range(1, expected_orders + 1)
    )
    dataset = LogicalDataset(
        name=name,
        seed=DATASET_SEED,
        base_instant=BASE_INSTANT,
        orders=orders,
        shipments=tuple(shipments),
        inboxes=tuple(inboxes),
        tracking_events=tuple(events),
        notifications=tuple(notifications),
    )
    validate_dataset(dataset)
    from benchmarks.semantic import validate_semantic_document

    validate_semantic_document(dataset.logical_document())
    from benchmarks.dataset_validation import validate_logical_dataset

    validate_logical_dataset(dataset)
    return dataset


def validate_dataset(dataset: LogicalDataset) -> None:
    """Enforce canonical matrices and referential invariants on generated rows."""
    expected_counts = {
        "demo": {
            "orders": 25,
            "shipments": 50,
            "carrier_event_inbox": 93,
            "tracking_events": 89,
            "notifications": 86,
        },
        "benchmark": {
            "orders": 1_000,
            "shipments": 1_500,
            "carrier_event_inbox": 15_000,
            "tracking_events": 15_000,
            "notifications": 2_998,
        },
    }[dataset.name]
    if dataset.counts != expected_counts:
        raise AssertionError(f"invalid {dataset.name} cardinalities: {dataset.counts}")
    if dataset.status_counts != {
        status.value: count for status, count in _STATUS_DISTRIBUTIONS[dataset.name]
    }:
        raise AssertionError("Shipment status matrix diverged")
    expected_per_carrier = len(dataset.shipments) // 2
    if dataset.carrier_counts != {
        "carrier-alpha": expected_per_carrier,
        "carrier-beta": expected_per_carrier,
    }:
        raise AssertionError("carrier distribution is not 50/50")
    if any(not order.recipient_email.endswith("@example.test") for order in dataset.orders):
        raise AssertionError("dataset contains a recipient outside example.test")
    if len({item.id for item in dataset.orders}) != len(dataset.orders):
        raise AssertionError("duplicate Order UUID")
    if len({item.id for item in dataset.shipments}) != len(dataset.shipments):
        raise AssertionError("duplicate Shipment UUID")
    if len({item.id for item in dataset.inboxes}) != len(dataset.inboxes):
        raise AssertionError("duplicate inbox UUID")
    if len({item.external_event_id for item in dataset.inboxes}) != len(dataset.inboxes):
        raise AssertionError("duplicate external event ID")
    if dataset.name == "demo":
        inbox_statuses = Counter(item.status for item in dataset.inboxes)
        if inbox_statuses != {"PROCESSED": 89, "REJECTED": 2, "RECEIVED": 2}:
            raise AssertionError(f"demo inbox status matrix diverged: {dict(inbox_statuses)}")
    expected_results = {
        result.value: count for result, count in _RESULT_DISTRIBUTIONS[dataset.name].items()
    }
    if dataset.tracking_result_counts != expected_results:
        raise AssertionError(
            f"invalid {dataset.name} application-result matrix: {dataset.tracking_result_counts}"
        )
    processed = [item for item in dataset.inboxes if item.status == "PROCESSED"]
    if len(processed) != len(dataset.tracking_events):
        raise AssertionError("each processed inbox must have one TrackingEvent")
    applied = [
        item
        for item in dataset.tracking_events
        if item.application_result == ShipmentApplicationResult.APPLIED.value
    ]
    if len(applied) != len(dataset.notifications):
        raise AssertionError("each APPLIED TrackingEvent must have one Notification")
    event_ids = {item.id for item in dataset.tracking_events}
    if {item.tracking_event_id for item in dataset.notifications} - event_ids:
        raise AssertionError("Notification references an unknown TrackingEvent")
    if dataset.name == "benchmark":
        mutable = [
            item
            for item in dataset.shipments
            if item.status
            in {ShipmentStatus.IN_TRANSIT.value, ShipmentStatus.OUT_FOR_DELIVERY.value}
        ]
        if len(mutable) != 376:
            raise AssertionError("benchmark mutable component must contain 376 Shipments")


def benchmark_cohorts(dataset: LogicalDataset) -> dict[str, object]:
    """Derive Locust cohorts only from the authenticated logical dataset."""
    benchmark = dataset
    if benchmark.name != "benchmark":
        raise ValueError("benchmark cohorts require the benchmark dataset")
    grouped: dict[tuple[str, str], list[ShipmentRow]] = defaultdict(list)
    for shipment in benchmark.shipments:
        if shipment.status in {
            ShipmentStatus.IN_TRANSIT.value,
            ShipmentStatus.OUT_FOR_DELIVERY.value,
        }:
            grouped[(shipment.carrier_code, shipment.status)].append(shipment)

    warmup: list[dict[str, object]] = []
    measurement: list[dict[str, object]] = []
    for key in sorted(grouped):
        rows = sorted(grouped[key], key=lambda item: str(item.id))
        if len(rows) != 94:
            raise AssertionError(f"mutable carrier/status group {key} must contain 94 rows")
        warmup.extend(_cohort_slot("warmup", item) for item in rows[:47])
        measurement.extend(_cohort_slot("measure", item) for item in rows[47:])

    mutable_ids = {str(item["shipment_id"]) for item in warmup + measurement}
    timeline = [
        {
            "shipment_id": str(item.id),
            "carrier_code": item.carrier_code,
            "tracking_code": item.tracking_code,
            "initial_status": item.status,
        }
        for item in benchmark.shipments
        if str(item.id) not in mutable_ids
    ]
    return {
        "mutable_component": {
            "statuses": [
                ShipmentStatus.IN_TRANSIT.value,
                ShipmentStatus.OUT_FOR_DELIVERY.value,
            ],
            "maximum_users_per_phase": 188,
            "warmup": sorted(warmup, key=lambda item: cast(str, item["slot_id"])),
            "measurement": sorted(measurement, key=lambda item: cast(str, item["slot_id"])),
        },
        "timeline_read_cohort": sorted(timeline, key=lambda item: item["shipment_id"]),
    }


def benchmark_manifest(dataset: LogicalDataset | None = None) -> dict[str, object]:
    """Return the integral frozen document retained for compatibility with tooling."""
    benchmark = dataset or generate_dataset("benchmark")
    if benchmark.name != "benchmark":
        raise ValueError("benchmark manifest requires the benchmark dataset")
    return benchmark.logical_document()


def write_benchmark_artifacts(directory: Path) -> tuple[Path, Path, str]:
    """Write exactly the canonical hash input and its non-self-referential sidecar."""
    dataset = generate_dataset("benchmark")
    manifest_path = directory / BENCHMARK_MANIFEST_NAME
    hash_path = directory / BENCHMARK_HASH_NAME
    directory.mkdir(parents=True, exist_ok=True)
    canonical = dataset.canonical_bytes()
    manifest_path.write_bytes(canonical)
    digest = hashlib.sha256(canonical).hexdigest()
    hash_path.write_text(f"{digest}\n", encoding="ascii", newline="\n")
    return manifest_path, hash_path, digest


def verify_benchmark_artifacts(directory: Path) -> str:
    """Authenticate canonical persisted bytes and compare them with the generator."""
    dataset = generate_dataset("benchmark")
    expected_manifest = dataset.canonical_bytes()
    manifest_path = directory / BENCHMARK_MANIFEST_NAME
    hash_path = directory / BENCHMARK_HASH_NAME
    persisted = manifest_path.read_bytes()
    if persisted != expected_manifest:
        raise ValueError("frozen benchmark manifest diverges from the deterministic generator")
    recorded = hash_path.read_text(encoding="ascii").strip()
    actual = hashlib.sha256(persisted).hexdigest()
    if recorded != actual:
        raise ValueError("frozen benchmark logical hash diverges from the generated dataset")
    return actual


def load_frozen_benchmark_artifact(
    path: Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[LogicalDataset, dict[str, object], str]:
    """Authenticate, parse and independently validate the only accepted dataset artifact."""
    persisted_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    document, actual = load_authenticated_document(
        path,
        expected_sha256=expected_sha256 or persisted_digest,
    )
    dataset = logical_dataset_from_document(document)
    from benchmarks.dataset_validation import validate_artifact_cohorts, validate_logical_dataset

    validate_logical_dataset(dataset)
    validate_artifact_cohorts(dataset, document)
    return dataset, document, actual


def _expanded_statuses(name: DatasetName) -> list[ShipmentStatus]:
    return [status for status, count in _STATUS_DISTRIBUTIONS[name] for _ in range(count)]


def _order_index(name: DatasetName, shipment_index: int) -> int:
    if name == "demo":
        return ((shipment_index - 1) // 2) + 1
    if shipment_index <= 1_000:
        return ((shipment_index - 1) // 2) + 1
    return shipment_index - 500


def _applied_path(
    name: DatasetName,
    status: ShipmentStatus,
    shipment_index: int,
) -> tuple[ShipmentStatus, ...]:
    if name == "benchmark":
        return _BENCHMARK_APPLIED_PATHS[status]
    if status is ShipmentStatus.DELIVERED:
        # One complete four-edge journey and five three-edge jumps derive 19 events.
        if shipment_index == 27:
            return _BENCHMARK_APPLIED_PATHS[status]
        return (
            ShipmentStatus.POSTED,
            ShipmentStatus.IN_TRANSIT,
            ShipmentStatus.DELIVERED,
        )
    if status is ShipmentStatus.RETURNED:
        return (
            ShipmentStatus.POSTED,
            ShipmentStatus.IN_TRANSIT,
            ShipmentStatus.RETURNED,
        )
    return _BENCHMARK_APPLIED_PATHS[status]


def _extra_tracking_count(name: DatasetName, shipment_index: int, applied_count: int) -> int:
    if name == "benchmark":
        return 10 - applied_count
    return 1 if shipment_index in {8, 15, 21} else 0


def _extra_result(
    name: DatasetName,
    shipment_index: int,
    final_status: ShipmentStatus,
    remaining: Counter[ShipmentApplicationResult],
) -> ShipmentApplicationResult:
    if name == "demo":
        return {
            8: ShipmentApplicationResult.NO_STATE_CHANGE,
            15: ShipmentApplicationResult.IGNORED_STALE,
            21: ShipmentApplicationResult.IGNORED_INVALID_TRANSITION,
        }[shipment_index]
    repeatable = final_status not in {ShipmentStatus.PENDING, ShipmentStatus.CANCELLED}
    if repeatable and remaining[ShipmentApplicationResult.NO_STATE_CHANGE] > 0:
        return ShipmentApplicationResult.NO_STATE_CHANGE
    if repeatable and remaining[ShipmentApplicationResult.IGNORED_STALE] > 0:
        return ShipmentApplicationResult.IGNORED_STALE
    return ShipmentApplicationResult.IGNORED_INVALID_TRANSITION


def _target_for_result(
    result: ShipmentApplicationResult,
    current: ShipmentStatus,
) -> ShipmentStatus:
    if result in {
        ShipmentApplicationResult.NO_STATE_CHANGE,
        ShipmentApplicationResult.IGNORED_STALE,
    }:
        return current
    if result is not ShipmentApplicationResult.IGNORED_INVALID_TRANSITION:
        raise AssertionError("extra event cannot request an APPLIED result")
    if current is ShipmentStatus.PENDING:
        return ShipmentStatus.RETURNED
    if current is ShipmentStatus.POSTED:
        raise AssertionError("POSTED has no distinct prohibited carrier status")
    return ShipmentStatus.POSTED


def _append_event(
    *,
    name: DatasetName,
    shipment_index: int,
    event_sequence: int,
    target: ShipmentStatus,
    expected_result: ShipmentApplicationResult,
    domain_shipment: Shipment,
    carrier_id: UUID,
    carrier_code: str,
    adapter_key: str,
    recipient: str,
    inboxes: list[InboxRow],
    events: list[TrackingEventRow],
    notifications: list[NotificationRow],
) -> None:
    external_event_id = f"ff-{name}-{adapter_key}-s{shipment_index:04d}-e{event_sequence:02d}"
    scheduled_occurred_at = BASE_INSTANT + timedelta(
        days=2, seconds=(shipment_index * 20) + event_sequence
    )
    occurred_at = (
        domain_shipment.status_occurred_at - timedelta(microseconds=1)
        if expected_result is ShipmentApplicationResult.IGNORED_STALE
        else scheduled_occurred_at
    )
    received_at = BASE_INSTANT + timedelta(days=3, seconds=(shipment_index * 20) + event_sequence)
    external_status = _EXTERNAL_STATUS[adapter_key][target]
    payload = _carrier_payload(
        adapter_key=adapter_key,
        event_id=external_event_id,
        tracking_code=domain_shipment.tracking_code,
        external_status=external_status,
        occurred_at=occurred_at,
    )
    normalized = normalize_carrier_event(adapter_key, payload)
    if (
        normalized.external_event_id != external_event_id
        or normalized.tracking_code != domain_shipment.tracking_code
        or normalized.canonical_status.value != target.value
    ):
        raise AssertionError("generated carrier payload does not normalize to its logical event")
    raw_body = _canonical_json_bytes(payload)
    transition = domain_shipment.apply_external_status(
        target,
        occurred_at=occurred_at,
        received_at=received_at,
        external_event_id=external_event_id,
    )
    if transition.result is not expected_result:
        raise AssertionError(
            f"generated {name} event expected {expected_result.value}, "
            f"canonical machine returned {transition.result.value}"
        )
    inbox_id = deterministic_uuid(f"{name}:inbox:{shipment_index}:{event_sequence}")
    tracking_id = deterministic_uuid(f"{name}:tracking:{shipment_index}:{event_sequence}")
    inboxes.append(
        InboxRow(
            id=inbox_id,
            carrier_id=carrier_id,
            external_event_id=external_event_id,
            payload_sha256=hashlib.sha256(raw_body).hexdigest(),
            raw_body=raw_body,
            parsed_payload=payload,
            received_at=received_at,
            status="PROCESSED",
            error_code=None,
            error_detail=None,
            processed_at=received_at + timedelta(microseconds=1),
            request_id=deterministic_uuid(f"{name}:request:{shipment_index}:{event_sequence}"),
        )
    )
    events.append(
        TrackingEventRow(
            id=tracking_id,
            inbox_event_id=inbox_id,
            shipment_id=domain_shipment.id,
            carrier_id=carrier_id,
            external_status=external_status,
            canonical_status=target.value,
            description=normalized.description,
            location=normalized.location,
            occurred_at=occurred_at,
            received_at=received_at,
            application_result=transition.result.value,
            previous_shipment_status=transition.previous_status.value,
            resulting_shipment_status=transition.resulting_status.value,
            created_at=received_at + timedelta(microseconds=1),
        )
    )
    if transition.result is ShipmentApplicationResult.APPLIED:
        built = build_notification(
            notification_id=deterministic_uuid(
                f"{name}:notification:{shipment_index}:{event_sequence}"
            ),
            shipment_id=domain_shipment.id,
            tracking_event_id=tracking_id,
            recipient=recipient,
            resulting_status=transition.resulting_status.value,
            created_at=received_at + timedelta(microseconds=2),
        )
        notifications.append(
            NotificationRow(
                id=built.id,
                shipment_id=built.shipment_id,
                tracking_event_id=built.tracking_event_id,
                channel=built.channel.value,
                recipient=built.recipient,
                template_key=built.template_key,
                message=built.message,
                status=built.status.value,
                error_detail=built.error_detail,
                created_at=built.created_at,
                simulated_at=built.simulated_at,
            )
        )


def _append_demo_operational_inboxes(inboxes: list[InboxRow]) -> None:
    """Add two permanent rejections and two authenticated pending inboxes."""
    for index in range(1, 3):
        carrier_id, _carrier_code, adapter_key = _CARRIERS[(index - 1) % 2]
        event_id = f"ff-demo-{adapter_key}-rejected-{index:02d}"
        occurred_at = BASE_INSTANT + timedelta(days=4, seconds=index)
        payload = _carrier_payload(
            adapter_key=adapter_key,
            event_id=event_id,
            tracking_code=f"{adapter_key.upper()}-DEMO-MISSING-{index:02d}",
            external_status="UNKNOWN_SYNTHETIC",
            occurred_at=occurred_at,
        )
        raw_body = _canonical_json_bytes(payload)
        inboxes.append(
            InboxRow(
                id=deterministic_uuid(f"demo:rejected-inbox:{index}"),
                carrier_id=carrier_id,
                external_event_id=event_id,
                payload_sha256=hashlib.sha256(raw_body).hexdigest(),
                raw_body=raw_body,
                parsed_payload=payload,
                received_at=occurred_at + timedelta(seconds=1),
                status="REJECTED",
                error_code="UNKNOWN_EXTERNAL_STATUS",
                error_detail="The synthetic carrier status is not supported.",
                processed_at=occurred_at + timedelta(seconds=1, microseconds=1),
                request_id=deterministic_uuid(f"demo:rejected-request:{index}"),
            )
        )
    for index in range(1, 3):
        carrier_id, _carrier_code, adapter_key = _CARRIERS[(index - 1) % 2]
        event_id = f"ff-demo-{adapter_key}-received-{index:02d}"
        occurred_at = BASE_INSTANT + timedelta(days=4, minutes=1, seconds=index)
        payload = _carrier_payload(
            adapter_key=adapter_key,
            event_id=event_id,
            tracking_code=_tracking_code("demo", _CARRIERS[(index - 1) % 2][1], index),
            external_status=_EXTERNAL_STATUS[adapter_key][ShipmentStatus.IN_TRANSIT],
            occurred_at=occurred_at,
        )
        raw_body = _canonical_json_bytes(payload)
        inboxes.append(
            InboxRow(
                id=deterministic_uuid(f"demo:received-inbox:{index}"),
                carrier_id=carrier_id,
                external_event_id=event_id,
                payload_sha256=hashlib.sha256(raw_body).hexdigest(),
                raw_body=raw_body,
                parsed_payload=None,
                received_at=occurred_at + timedelta(seconds=1),
                status="RECEIVED",
                error_code=None,
                error_detail=None,
                processed_at=None,
                request_id=deterministic_uuid(f"demo:received-request:{index}"),
            )
        )


def _carrier_payload(
    *,
    adapter_key: str,
    event_id: str,
    tracking_code: str,
    external_status: str,
    occurred_at: datetime,
) -> dict[str, object]:
    instant = _iso_datetime(occurred_at)
    if adapter_key == "alpha":
        return {
            "eventId": event_id,
            "trackingCode": tracking_code,
            "status": external_status,
            "eventDate": instant,
            "city": "Synthetic City",
            "description": "Deterministic synthetic carrier event",
        }
    return {
        "id": event_id,
        "tracking_number": tracking_code,
        "event": {
            "type": external_status,
            "occurred_at": instant,
            "details": "Deterministic synthetic carrier event",
        },
        "location": {"city": "Synthetic City", "state": "SP"},
    }


def _order_row(
    name: DatasetName,
    order_index: int,
    shipment_statuses: list[ShipmentStatus],
) -> OrderRow:
    non_cancelled = [
        status for status in shipment_statuses if status is not ShipmentStatus.CANCELLED
    ]
    status = (
        "FULFILLED"
        if non_cancelled and all(item is ShipmentStatus.DELIVERED for item in non_cancelled)
        else "CONFIRMED"
    )
    created_at = BASE_INSTANT + timedelta(seconds=order_index)
    updated_at = created_at + timedelta(days=3) if status == "FULFILLED" else created_at
    return OrderRow(
        id=deterministic_uuid(f"{name}:order:{order_index}"),
        external_reference=f"{name.upper()}-ORDER-{order_index:04d}",
        recipient_name=f"Synthetic Recipient {order_index:04d}",
        recipient_email=_recipient_email(name, order_index),
        recipient_postal_code=f"{order_index % 100_000:05d}-000",
        recipient_city="Synthetic City",
        recipient_state="SP",
        status=status,
        created_at=created_at,
        updated_at=updated_at,
    )


def _recipient_email(name: DatasetName, order_index: int) -> str:
    return f"{name}-order-{order_index:04d}@example.test"


def _tracking_code(name: DatasetName, carrier_code: str, shipment_index: int) -> str:
    carrier = "ALPHA" if carrier_code == "carrier-alpha" else "BETA"
    return f"{carrier}-{name.upper()}-{shipment_index:06d}"


def _cohort_slot(phase: str, shipment: ShipmentRow) -> dict[str, object]:
    short_carrier = "alpha" if shipment.carrier_code == "carrier-alpha" else "beta"
    short_status = "it" if shipment.status == ShipmentStatus.IN_TRANSIT.value else "ofd"
    return {
        "slot_id": f"{phase}-{short_carrier}-{short_status}-{shipment.tracking_code[-6:]}",
        "shipment_id": str(shipment.id),
        "carrier_code": shipment.carrier_code,
        "tracking_code": shipment.tracking_code,
        "initial_status": shipment.status,
        "initial_occurred_at": _iso_datetime(shipment.status_occurred_at),
    }


def _database_row(
    row: DatasetRow,
    *,
    excluded: frozenset[str] = frozenset(),
) -> dict[str, object]:
    return {key: value for key, value in asdict(row).items() if key not in excluded}


def _canonical_row(row: DatasetRow) -> dict[str, object]:
    return {key: _canonical_value(value) for key, value in asdict(row).items()}


def _canonical_rows(rows: tuple[DatasetRow, ...]) -> list[dict[str, object]]:
    """Canonicalize a table independently from generator iteration order."""
    return sorted((_canonical_row(item) for item in rows), key=lambda item: cast(str, item["id"]))


def _canonical_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _iso_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return canonical_json_bytes(value)


def _iso_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("canonical timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def logical_dataset_from_document(document: dict[str, object]) -> LogicalDataset:
    """Rehydrate an authenticated logical artifact without consulting the generator."""
    metadata = _required_mapping(document, "metadata")
    tables = _required_mapping(document, "tables")
    if metadata.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise ValueError("unsupported benchmark dataset schema version")
    if metadata.get("dataset") != "benchmark":
        raise ValueError("campaign artifact must contain the benchmark dataset")
    if metadata.get("seed") != DATASET_SEED:
        raise ValueError("benchmark dataset seed is not the frozen value")
    try:
        base_instant = _parse_datetime(metadata["base_instant"])
        orders = tuple(_order_from_json(item) for item in _required_rows(tables, "orders"))
        shipments = tuple(_shipment_from_json(item) for item in _required_rows(tables, "shipments"))
        inboxes = tuple(
            _inbox_from_json(item) for item in _required_rows(tables, "carrier_event_inbox")
        )
        tracking_events = tuple(
            _tracking_from_json(item) for item in _required_rows(tables, "tracking_events")
        )
        notifications = tuple(
            _notification_from_json(item) for item in _required_rows(tables, "notifications")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("frozen benchmark artifact has an invalid logical table row") from exc
    dataset = LogicalDataset(
        name="benchmark",
        seed=DATASET_SEED,
        base_instant=base_instant,
        orders=orders,
        shipments=shipments,
        inboxes=inboxes,
        tracking_events=tracking_events,
        notifications=notifications,
    )
    if metadata.get("counts") != dataset.counts:
        raise ValueError("frozen benchmark metadata counts diverge from its tables")
    if metadata.get("shipment_statuses") != dataset.status_counts:
        raise ValueError("frozen benchmark metadata statuses diverge from its tables")
    if metadata.get("shipment_carriers") != dataset.carrier_counts:
        raise ValueError("frozen benchmark metadata carriers diverge from its tables")
    return dataset


def _required_mapping(document: dict[str, object], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"frozen benchmark artifact has no {key} object")
    return cast(dict[str, Any], value)


def _required_rows(tables: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = tables.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"frozen benchmark artifact table {key} is invalid")
    rows = cast(list[dict[str, Any]], value)
    if rows != sorted(rows, key=lambda item: str(item.get("id", ""))):
        raise ValueError(f"frozen benchmark artifact table {key} is not canonically ordered")
    return rows


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("logical timestamp must be canonical UTC text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or _iso_datetime(parsed) != value:
        raise ValueError("logical timestamp is not canonical")
    return parsed


def _parse_optional_datetime(value: object) -> datetime | None:
    return None if value is None else _parse_datetime(value)


def _parse_optional_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("logical date must be ISO text")
    return date.fromisoformat(value)


def _parse_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("logical UUID must be text")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("logical UUID is not canonical")
    return parsed


def _parse_optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("logical optional string is invalid")


def _order_from_json(row: dict[str, Any]) -> OrderRow:
    return OrderRow(
        id=_parse_uuid(row["id"]),
        external_reference=str(row["external_reference"]),
        recipient_name=str(row["recipient_name"]),
        recipient_email=str(row["recipient_email"]),
        recipient_postal_code=str(row["recipient_postal_code"]),
        recipient_city=str(row["recipient_city"]),
        recipient_state=str(row["recipient_state"]),
        status=str(row["status"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _shipment_from_json(row: dict[str, Any]) -> ShipmentRow:
    return ShipmentRow(
        id=_parse_uuid(row["id"]),
        order_id=_parse_uuid(row["order_id"]),
        carrier_id=_parse_uuid(row["carrier_id"]),
        carrier_code=str(row["carrier_code"]),
        tracking_code=str(row["tracking_code"]),
        status=str(row["status"]),
        status_occurred_at=_parse_datetime(row["status_occurred_at"]),
        status_event_received_at=_parse_optional_datetime(row["status_event_received_at"]),
        status_external_event_id=_parse_optional_string(row["status_external_event_id"]),
        estimated_delivery_date=_parse_optional_date(row["estimated_delivery_date"]),
        shipped_at=_parse_optional_datetime(row["shipped_at"]),
        delivered_at=_parse_optional_datetime(row["delivered_at"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _inbox_from_json(row: dict[str, Any]) -> InboxRow:
    encoded = row["raw_body"]
    if not isinstance(encoded, dict) or not isinstance(encoded.get("base64"), str):
        raise ValueError("logical raw_body must use canonical base64")
    parsed_payload = row["parsed_payload"]
    if parsed_payload is not None and not isinstance(parsed_payload, dict):
        raise ValueError("logical parsed_payload must be an object or null")
    return InboxRow(
        id=_parse_uuid(row["id"]),
        carrier_id=_parse_uuid(row["carrier_id"]),
        external_event_id=str(row["external_event_id"]),
        payload_sha256=str(row["payload_sha256"]),
        raw_body=base64.b64decode(encoded["base64"], validate=True),
        parsed_payload=cast(dict[str, object] | None, parsed_payload),
        received_at=_parse_datetime(row["received_at"]),
        status=str(row["status"]),
        error_code=_parse_optional_string(row["error_code"]),
        error_detail=_parse_optional_string(row["error_detail"]),
        processed_at=_parse_optional_datetime(row["processed_at"]),
        request_id=_parse_uuid(row["request_id"]),
    )


def _tracking_from_json(row: dict[str, Any]) -> TrackingEventRow:
    return TrackingEventRow(
        id=_parse_uuid(row["id"]),
        inbox_event_id=_parse_uuid(row["inbox_event_id"]),
        shipment_id=_parse_uuid(row["shipment_id"]),
        carrier_id=_parse_uuid(row["carrier_id"]),
        external_status=str(row["external_status"]),
        canonical_status=str(row["canonical_status"]),
        description=_parse_optional_string(row["description"]),
        location=_parse_optional_string(row["location"]),
        occurred_at=_parse_datetime(row["occurred_at"]),
        received_at=_parse_datetime(row["received_at"]),
        application_result=str(row["application_result"]),
        previous_shipment_status=_parse_optional_string(row["previous_shipment_status"]),
        resulting_shipment_status=str(row["resulting_shipment_status"]),
        created_at=_parse_datetime(row["created_at"]),
    )


def _notification_from_json(row: dict[str, Any]) -> NotificationRow:
    return NotificationRow(
        id=_parse_uuid(row["id"]),
        shipment_id=_parse_uuid(row["shipment_id"]),
        tracking_event_id=_parse_uuid(row["tracking_event_id"]),
        channel=str(row["channel"]),
        recipient=str(row["recipient"]),
        template_key=str(row["template_key"]),
        message=str(row["message"]),
        status=str(row["status"]),
        error_detail=_parse_optional_string(row["error_detail"]),
        created_at=_parse_datetime(row["created_at"]),
        simulated_at=_parse_optional_datetime(row["simulated_at"]),
    )


if __name__ == "__main__":
    paths = write_benchmark_artifacts(Path(__file__).with_name("datasets"))
    print(f"wrote {paths[0].name} and {paths[1].name}; logical SHA-256 {paths[2]}")
