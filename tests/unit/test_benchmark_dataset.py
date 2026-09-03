"""Deterministic dataset matrices, canonical hash, and privacy invariants."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from benchmarks.dataset import (
    BENCHMARK_HASH_NAME,
    BENCHMARK_MANIFEST_NAME,
    _canonical_json_bytes,
    _carrier_payload,
    deterministic_uuid,
    generate_dataset,
    load_frozen_benchmark_artifact,
    verify_benchmark_artifacts,
    write_benchmark_artifacts,
)
from benchmarks.dataset_validation import validate_logical_dataset
from benchmarks.semantic import (
    FrozenShipmentState,
    apply_frozen_status,
    validate_semantic_document,
)

from fulfillflow.shipments.domain import Shipment, ShipmentStatus


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "demo",
            {
                "orders": 25,
                "shipments": 50,
                "carrier_event_inbox": 93,
                "tracking_events": 89,
                "notifications": 86,
            },
        ),
        (
            "benchmark",
            {
                "orders": 1_000,
                "shipments": 1_500,
                "carrier_event_inbox": 15_000,
                "tracking_events": 15_000,
                "notifications": 2_998,
            },
        ),
    ],
)
def test_dataset_cardinalities_are_derived(name: str, expected: dict[str, int]) -> None:
    dataset = generate_dataset(name)  # type: ignore[arg-type]

    assert dataset.counts == expected
    applied = sum(item.application_result == "APPLIED" for item in dataset.tracking_events)
    assert applied == len(dataset.notifications)
    assert sum(item.status == "PROCESSED" for item in dataset.inboxes) == len(
        dataset.tracking_events
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "demo",
            {
                "APPLIED": 86,
                "NO_STATE_CHANGE": 1,
                "IGNORED_STALE": 1,
                "IGNORED_INVALID_TRANSITION": 1,
            },
        ),
        (
            "benchmark",
            {
                "APPLIED": 2_998,
                "NO_STATE_CHANGE": 3_002,
                "IGNORED_STALE": 2_814,
                "IGNORED_INVALID_TRANSITION": 6_186,
            },
        ),
    ],
)
def test_application_result_matrix_is_exact(name: str, expected: dict[str, int]) -> None:
    dataset = generate_dataset(name)  # type: ignore[arg-type]

    assert dataset.tracking_result_counts == expected
    assert sum(dataset.tracking_result_counts.values()) == len(dataset.tracking_events)


def test_benchmark_matrix_has_balanced_carriers_and_mutable_component() -> None:
    dataset = generate_dataset("benchmark")

    assert dataset.status_counts == {
        "CANCELLED": 187,
        "DELIVERED": 187,
        "EXCEPTION": 187,
        "IN_TRANSIT": 188,
        "OUT_FOR_DELIVERY": 188,
        "PENDING": 188,
        "POSTED": 188,
        "RETURNED": 187,
    }
    assert dataset.carrier_counts == {"carrier-alpha": 750, "carrier-beta": 750}
    assert set(Counter(event.shipment_id for event in dataset.tracking_events).values()) == {10}
    mutable = [
        shipment
        for shipment in dataset.shipments
        if shipment.status in {"IN_TRANSIT", "OUT_FOR_DELIVERY"}
    ]
    assert len(mutable) == 376


def test_demo_has_exact_processed_rejected_received_matrix() -> None:
    dataset = generate_dataset("demo")

    assert Counter(item.status for item in dataset.inboxes) == {
        "PROCESSED": 89,
        "REJECTED": 2,
        "RECEIVED": 2,
    }
    received = [item for item in dataset.inboxes if item.status == "RECEIVED"]
    assert all(item.processed_at is None and item.error_code is None for item in received)
    assert {item.id for item in received}.isdisjoint(
        event.inbox_event_id for event in dataset.tracking_events
    )


def test_deterministic_uuid_uses_rfc4122_variant_and_version_four() -> None:
    first = deterministic_uuid("proof")
    second = deterministic_uuid("proof")

    assert first == second
    assert first.version == 4
    assert first.variant == "specified in RFC 4122"


def test_dataset_is_deterministic_private_and_raw_hashes_match() -> None:
    first = generate_dataset("demo")
    second = generate_dataset("demo")

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.logical_hash() == second.logical_hash()
    assert all(order.recipient_email.endswith("@example.test") for order in first.orders)
    assert all(
        inbox.payload_sha256 == hashlib.sha256(inbox.raw_body).hexdigest()
        for inbox in first.inboxes
    )


def test_canonical_document_is_independent_from_generation_order() -> None:
    dataset = generate_dataset("benchmark")
    reversed_dataset = replace(
        dataset,
        orders=tuple(reversed(dataset.orders)),
        shipments=tuple(reversed(dataset.shipments)),
        inboxes=tuple(reversed(dataset.inboxes)),
        tracking_events=tuple(reversed(dataset.tracking_events)),
        notifications=tuple(reversed(dataset.notifications)),
    )

    assert reversed_dataset.canonical_bytes() == dataset.canonical_bytes()
    assert reversed_dataset.logical_hash() == dataset.logical_hash()


def test_hash_is_separate_and_frozen_artifacts_regenerate_exactly(tmp_path: Path) -> None:
    manifest, digest_file, digest = write_benchmark_artifacts(tmp_path)

    assert manifest.name == BENCHMARK_MANIFEST_NAME
    assert digest_file.name == BENCHMARK_HASH_NAME
    persisted = manifest.read_bytes()
    assert digest not in persisted.decode("utf-8")
    assert hashlib.sha256(persisted).hexdigest() == digest
    assert digest_file.read_text(encoding="ascii").strip() == digest
    assert verify_benchmark_artifacts(tmp_path) == digest
    loaded, document, authenticated = load_frozen_benchmark_artifact(
        manifest, expected_sha256=digest
    )
    assert loaded.canonical_bytes() == persisted
    assert document == loaded.logical_document()
    assert authenticated == digest


def test_committed_benchmark_artifacts_match_generator() -> None:
    assert verify_benchmark_artifacts(Path("benchmarks/datasets")) == (
        "5897d7441f73fec77d98ff97196aff0becc3f301e45c708febff493d8f4a63bf"
    )


def test_mutated_notification_relation_is_rejected_with_unchanged_counts_and_valid_fks() -> None:
    dataset = generate_dataset("demo")
    non_applied = next(
        item for item in dataset.tracking_events if item.application_result != "APPLIED"
    )
    mutated = replace(
        dataset,
        notifications=(
            replace(
                dataset.notifications[0],
                tracking_event_id=non_applied.id,
                shipment_id=non_applied.shipment_id,
            ),
            *dataset.notifications[1:],
        ),
    )

    with pytest.raises(ValueError, match=r"Notifications|Notification"):
        validate_logical_dataset(mutated)


def test_applied_notification_with_another_existing_shipment_is_rejected() -> None:
    dataset = generate_dataset("demo")
    notification = dataset.notifications[0]
    another_shipment = next(
        item for item in dataset.shipments if item.id != notification.shipment_id
    )
    mutated = replace(
        dataset,
        notifications=(
            replace(notification, shipment_id=another_shipment.id),
            *dataset.notifications[1:],
        ),
    )

    assert len(mutated.notifications) == len(dataset.notifications)
    with pytest.raises(ValueError, match="Notification Shipment"):
        validate_logical_dataset(mutated)
    with pytest.raises(ValueError, match="Notification Shipment"):
        validate_semantic_document(mutated.logical_document())


def test_mutated_order_fulfillment_is_rejected_without_changing_relations() -> None:
    dataset = generate_dataset("demo")
    index = next(index for index, item in enumerate(dataset.orders) if item.status == "FULFILLED")
    orders = list(dataset.orders)
    orders[index] = replace(orders[index], status="CONFIRMED")

    with pytest.raises(ValueError, match="FULFILLED"):
        validate_logical_dataset(replace(dataset, orders=tuple(orders)))


def test_swapped_stale_and_no_state_labels_are_rejected_with_counts_unchanged() -> None:
    dataset = generate_dataset("demo")
    events = list(dataset.tracking_events)
    no_state_index = next(
        index for index, item in enumerate(events) if item.application_result == "NO_STATE_CHANGE"
    )
    stale_index = next(
        index for index, item in enumerate(events) if item.application_result == "IGNORED_STALE"
    )
    events[no_state_index] = replace(events[no_state_index], application_result="IGNORED_STALE")
    events[stale_index] = replace(events[stale_index], application_result="NO_STATE_CHANGE")
    mutated = replace(dataset, tracking_events=tuple(events))

    assert mutated.tracking_result_counts == dataset.tracking_result_counts
    with pytest.raises(ValueError, match=r"NO_STATE_CHANGE|IGNORED_STALE|canonical"):
        validate_logical_dataset(mutated)


def test_invalid_transition_with_a_stale_key_is_rejected_semantically() -> None:
    dataset = generate_dataset("benchmark")
    shipments = {item.id: item for item in dataset.shipments}
    event_index = next(
        index
        for index, item in enumerate(dataset.tracking_events)
        if item.application_result == "IGNORED_INVALID_TRANSITION"
        and shipments[item.shipment_id].status not in {"PENDING", "CANCELLED"}
    )
    event = dataset.tracking_events[event_index]
    shipment = shipments[event.shipment_id]
    stale_occurred_at = shipment.status_occurred_at - timedelta(microseconds=1)
    inbox_index = next(
        index for index, item in enumerate(dataset.inboxes) if item.id == event.inbox_event_id
    )
    inbox = dataset.inboxes[inbox_index]
    adapter_key = "alpha" if shipment.carrier_code == "carrier-alpha" else "beta"
    payload = _carrier_payload(
        adapter_key=adapter_key,
        event_id=inbox.external_event_id,
        tracking_code=shipment.tracking_code,
        external_status=event.external_status,
        occurred_at=stale_occurred_at,
    )
    raw_body = _canonical_json_bytes(payload)
    inboxes = list(dataset.inboxes)
    inboxes[inbox_index] = replace(
        inbox,
        parsed_payload=payload,
        raw_body=raw_body,
        payload_sha256=hashlib.sha256(raw_body).hexdigest(),
    )
    events = list(dataset.tracking_events)
    events[event_index] = replace(event, occurred_at=stale_occurred_at)
    mutated = replace(dataset, inboxes=tuple(inboxes), tracking_events=tuple(events))

    assert mutated.tracking_result_counts == dataset.tracking_result_counts
    with pytest.raises(ValueError, match="posterior key"):
        validate_logical_dataset(mutated)


def test_mutated_final_shipment_ordering_state_is_rejected_by_timeline_replay() -> None:
    dataset = generate_dataset("demo")
    shipment_index = next(
        index for index, item in enumerate(dataset.shipments) if item.status == "IN_TRANSIT"
    )
    shipments = list(dataset.shipments)
    shipment = shipments[shipment_index]
    shipments[shipment_index] = replace(
        shipment,
        status_occurred_at=shipment.status_occurred_at + timedelta(microseconds=1),
    )

    with pytest.raises(ValueError, match="final state"):
        validate_logical_dataset(replace(dataset, shipments=tuple(shipments)))


def test_frozen_semantic_machine_matches_every_canonical_v1_transition_edge() -> None:
    base = datetime(2026, 8, 28, tzinfo=UTC)
    for current in ShipmentStatus:
        for target in ShipmentStatus:
            canonical = Shipment(
                id=UUID("00000000-0000-4000-8000-000000000001"),
                order_id=UUID("00000000-0000-4000-8000-000000000002"),
                carrier_id=UUID("00000000-0000-4000-8000-000000000100"),
                tracking_code="PARITY-000001",
                status=current,
                status_occurred_at=base,
                status_event_received_at=base + timedelta(seconds=1),
                status_external_event_id="previous-event",
                estimated_delivery_date=None,
                shipped_at=None,
                delivered_at=None,
                created_at=base,
                updated_at=base,
            )
            frozen = FrozenShipmentState(
                status=current.value,
                status_occurred_at=base,
                status_event_received_at=base + timedelta(seconds=1),
                status_external_event_id="previous-event",
                shipped_at=None,
                delivered_at=None,
                updated_at=base,
            )
            occurred_at = base + timedelta(seconds=2)
            received_at = base + timedelta(seconds=3)

            canonical_result = canonical.apply_external_status(
                target,
                occurred_at=occurred_at,
                received_at=received_at,
                external_event_id="next-event",
            )
            frozen_result = apply_frozen_status(
                frozen,
                target.value,
                occurred_at=occurred_at,
                received_at=received_at,
                external_event_id="next-event",
            )

            assert frozen_result.result == canonical_result.result.value
            assert frozen_result.previous_status == canonical_result.previous_status.value
            assert frozen_result.resulting_status == canonical_result.resulting_status.value
            assert frozen.status == canonical.status.value
            assert frozen.status_occurred_at == canonical.status_occurred_at
            assert frozen.status_event_received_at == canonical.status_event_received_at
            assert frozen.status_external_event_id == canonical.status_external_event_id
            assert frozen.shipped_at == canonical.shipped_at
            assert frozen.delivered_at == canonical.delivered_at
            assert frozen.updated_at == canonical.updated_at
