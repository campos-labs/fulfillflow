"""Independent logical validation for generated and authenticated benchmark data."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from fulfillflow.shipments.public import Shipment, ShipmentApplicationResult, ShipmentStatus
from fulfillflow.tracking.public import normalize_carrier_event

if TYPE_CHECKING:
    from benchmarks.dataset import InboxRow, LogicalDataset, ShipmentRow, TrackingEventRow

_CARRIERS = {
    UUID("00000000-0000-4000-8000-000000000100"): ("carrier-alpha", "alpha"),
    UUID("00000000-0000-4000-8000-000000000101"): ("carrier-beta", "beta"),
}
_EVENT_RESULTS = {item.value for item in ShipmentApplicationResult}


def validate_logical_dataset(dataset: LogicalDataset) -> None:
    """Reconstruct every relation and state without trusting generator counters."""
    _validate_unique_identities(dataset)
    orders = {item.id: item for item in dataset.orders}
    shipments = {item.id: item for item in dataset.shipments}
    inboxes = {item.id: item for item in dataset.inboxes}
    events = {item.id: item for item in dataset.tracking_events}

    for shipment in dataset.shipments:
        if shipment.order_id not in orders:
            raise ValueError("Shipment references an unknown Order")
        carrier = _CARRIERS.get(shipment.carrier_id)
        if carrier is None or carrier[0] != shipment.carrier_code:
            raise ValueError("Shipment carrier code/id pair is inconsistent")
    for inbox in dataset.inboxes:
        if inbox.carrier_id not in _CARRIERS:
            raise ValueError("inbox references an unknown official Carrier")
        if hashlib.sha256(inbox.raw_body).hexdigest() != inbox.payload_sha256:
            raise ValueError("inbox payload hash does not match its authenticated raw bytes")
        if inbox.status == "PROCESSED":
            if inbox.parsed_payload is None or inbox.processed_at is None:
                raise ValueError("PROCESSED inbox lacks its parsed payload or completion time")
        elif inbox.status == "REJECTED":
            if not inbox.error_code or inbox.processed_at is None:
                raise ValueError("REJECTED inbox lacks its permanent error outcome")
        elif inbox.status == "RECEIVED":
            if inbox.processed_at is not None or inbox.error_code is not None:
                raise ValueError("RECEIVED inbox must remain authenticated and unfinished")
        else:
            raise ValueError("inbox contains an unsupported status")

    events_by_inbox = Counter(item.inbox_event_id for item in dataset.tracking_events)
    processed_ids = {item.id for item in dataset.inboxes if item.status == "PROCESSED"}
    if set(events_by_inbox) != processed_ids or set(events_by_inbox.values()) != {1}:
        raise ValueError("every PROCESSED inbox must own exactly one TrackingEvent")

    for event in dataset.tracking_events:
        event_inbox = inboxes.get(event.inbox_event_id)
        event_shipment = shipments.get(event.shipment_id)
        if event_inbox is None or event_shipment is None:
            raise ValueError("TrackingEvent has a broken logical foreign key")
        if (
            event.carrier_id != event_inbox.carrier_id
            or event.carrier_id != event_shipment.carrier_id
        ):
            raise ValueError("TrackingEvent carrier does not match its inbox and Shipment")
        if event.application_result not in _EVENT_RESULTS:
            raise ValueError("TrackingEvent contains an unsupported application result")
        _validate_normalized_payload(event, event_inbox, event_shipment)

    notifications_by_event = Counter(item.tracking_event_id for item in dataset.notifications)
    applied_ids = {
        item.id
        for item in dataset.tracking_events
        if item.application_result == ShipmentApplicationResult.APPLIED.value
    }
    if set(notifications_by_event) != applied_ids or set(notifications_by_event.values()) != {1}:
        raise ValueError("Notifications must correspond one-to-one only with APPLIED events")
    for notification in dataset.notifications:
        notification_event = events.get(notification.tracking_event_id)
        if notification_event is None or notification.shipment_id != notification_event.shipment_id:
            raise ValueError("Notification Shipment does not match its TrackingEvent")
        if notification_event.application_result != ShipmentApplicationResult.APPLIED.value:
            raise ValueError("non-APPLIED TrackingEvent cannot create a Notification")
        if not notification.recipient.endswith("@example.test"):
            raise ValueError("Notification recipient is not synthetic")

    events_by_shipment: dict[UUID, list[TrackingEventRow]] = defaultdict(list)
    for event in dataset.tracking_events:
        events_by_shipment[event.shipment_id].append(event)
    for shipment in dataset.shipments:
        _reconstruct_shipment(shipment, events_by_shipment[shipment.id], inboxes)
    _validate_orders(dataset)


def validate_artifact_cohorts(
    dataset: LogicalDataset,
    document: dict[str, object],
) -> None:
    """Require cohorts to be the exact canonical derivation of authenticated tables."""
    from benchmarks.dataset import benchmark_cohorts

    cohorts = document.get("cohorts")
    expected = benchmark_cohorts(dataset)
    if cohorts != expected:
        raise ValueError("frozen benchmark cohorts diverge from the authenticated Shipment rows")
    if not isinstance(cohorts, dict):
        raise ValueError("frozen benchmark cohorts must be an object")
    mutable = cohorts.get("mutable_component")
    timeline = cohorts.get("timeline_read_cohort")
    if not isinstance(mutable, dict) or not isinstance(timeline, list):
        raise ValueError("frozen benchmark cohort structure is invalid")
    warmup = mutable.get("warmup")
    measurement = mutable.get("measurement")
    if not isinstance(warmup, list) or not isinstance(measurement, list):
        raise ValueError("frozen mutable cohorts are invalid")
    all_slots = [*warmup, *measurement]
    slot_ids = [_cohort_text(item, "slot_id") for item in all_slots]
    normalized = [_normalize_slot(item) for item in slot_ids]
    if len(set(slot_ids)) != len(slot_ids) or len(set(normalized)) != len(normalized):
        raise ValueError("mutable cohorts contain a colliding slot identity")
    tracking_codes = [_cohort_text(item, "tracking_code") for item in all_slots]
    if len(set(tracking_codes)) != len(tracking_codes):
        raise ValueError("mutable cohorts contain a duplicate tracking code")
    shipment_ids = [_cohort_text(item, "shipment_id") for item in all_slots]
    if len(set(shipment_ids)) != len(shipment_ids):
        raise ValueError("warm-up and measurement cohorts overlap")
    timeline_ids = {_cohort_text(item, "shipment_id") for item in timeline}
    if timeline_ids & set(shipment_ids):
        raise ValueError("timeline cohort overlaps a mutable cohort")


def _validate_unique_identities(dataset: LogicalDataset) -> None:
    table_ids = [
        *(item.id for item in dataset.orders),
        *(item.id for item in dataset.shipments),
        *(item.id for item in dataset.inboxes),
        *(item.id for item in dataset.tracking_events),
        *(item.id for item in dataset.notifications),
    ]
    if len(set(table_ids)) != len(table_ids):
        raise ValueError("logical dataset contains a duplicate row UUID")
    _require_unique(
        (item.external_reference for item in dataset.orders), "Order external reference"
    )
    _require_unique((item.tracking_code for item in dataset.shipments), "Shipment tracking code")
    _require_unique((item.external_event_id for item in dataset.inboxes), "inbox external event ID")
    _require_unique((item.request_id for item in dataset.inboxes), "inbox request ID")
    for inbox in dataset.inboxes:
        event_id = inbox.external_event_id
        if not event_id.isascii() or not 1 <= len(event_id) <= 128:
            raise ValueError("external event ID violates the public ASCII identity contract")
    if any(not item.recipient_email.endswith("@example.test") for item in dataset.orders):
        raise ValueError("Order recipient is not synthetic")


def _require_unique(values: Iterable[Hashable], label: str) -> None:
    materialized = list(values)
    if len(set(materialized)) != len(materialized):
        raise ValueError(f"logical dataset contains a duplicate {label}")


def _validate_normalized_payload(
    event: TrackingEventRow,
    inbox: InboxRow,
    shipment: ShipmentRow,
) -> None:
    payload = inbox.parsed_payload
    if payload is None:
        raise ValueError("TrackingEvent inbox has no parsed payload")
    adapter_key = _CARRIERS[event.carrier_id][1]
    try:
        normalized = normalize_carrier_event(adapter_key, payload)
    except Exception as exc:
        raise ValueError("processed carrier payload no longer normalizes") from exc
    if (
        normalized.external_event_id != inbox.external_event_id
        or normalized.tracking_code != shipment.tracking_code
        or normalized.canonical_status.value != event.canonical_status
        or normalized.occurred_at != event.occurred_at
        or event.received_at != inbox.received_at
    ):
        raise ValueError("processed carrier payload diverges from its TrackingEvent")
    if event.created_at < event.received_at or inbox.processed_at is None:
        raise ValueError("processed event timestamps do not follow ingestion order")


def _reconstruct_shipment(
    shipment: ShipmentRow,
    events: list[TrackingEventRow],
    inboxes: Mapping[UUID, InboxRow],
) -> None:
    state = Shipment(
        id=shipment.id,
        order_id=shipment.order_id,
        carrier_id=shipment.carrier_id,
        tracking_code=shipment.tracking_code,
        status=ShipmentStatus.PENDING,
        status_occurred_at=shipment.created_at,
        status_event_received_at=None,
        status_external_event_id=None,
        estimated_delivery_date=shipment.estimated_delivery_date,
        shipped_at=None,
        delivered_at=None,
        created_at=shipment.created_at,
        updated_at=shipment.created_at,
    )
    if shipment.status == ShipmentStatus.CANCELLED.value:
        state.cancel(shipment.status_occurred_at)
    ordered = sorted(
        events,
        key=lambda item: (
            item.created_at,
            item.received_at,
            inboxes[item.inbox_event_id].external_event_id,
        ),
    )
    for event in ordered:
        inbox = inboxes[event.inbox_event_id]
        current_key = (
            None
            if state.status_event_received_at is None or state.status_external_event_id is None
            else (
                state.status_occurred_at,
                state.status_event_received_at,
                state.status_external_event_id,
            )
        )
        incoming_key = (event.occurred_at, event.received_at, inbox.external_event_id)
        target = ShipmentStatus(event.canonical_status)
        _validate_result_semantics(
            event.application_result,
            current=state.status,
            target=target,
            current_key=current_key,
            incoming_key=incoming_key,
        )
        transition = state.apply_external_status(
            target,
            occurred_at=event.occurred_at,
            received_at=event.received_at,
            external_event_id=inbox.external_event_id,
        )
        if (
            transition.result.value != event.application_result
            or transition.previous_status.value != event.previous_shipment_status
            or transition.resulting_status.value != event.resulting_shipment_status
        ):
            raise ValueError("TrackingEvent does not match the canonical Shipment transition")
    actual = (
        state.status.value,
        state.status_occurred_at,
        state.status_event_received_at,
        state.status_external_event_id,
        state.shipped_at,
        state.delivered_at,
        state.updated_at,
    )
    expected = (
        shipment.status,
        shipment.status_occurred_at,
        shipment.status_event_received_at,
        shipment.status_external_event_id,
        shipment.shipped_at,
        shipment.delivered_at,
        shipment.updated_at,
    )
    if actual != expected:
        raise ValueError("Shipment final state cannot be reconstructed from its timeline")


def _validate_result_semantics(
    result: str,
    *,
    current: ShipmentStatus,
    target: ShipmentStatus,
    current_key: tuple[datetime, datetime, str] | None,
    incoming_key: tuple[datetime, datetime, str],
) -> None:
    is_posterior = current_key is None or incoming_key > current_key
    if result == ShipmentApplicationResult.NO_STATE_CHANGE.value:
        if target is not current or not is_posterior:
            raise ValueError("NO_STATE_CHANGE must repeat the state with a posterior key")
    elif result == ShipmentApplicationResult.IGNORED_STALE.value:
        if current_key is None or incoming_key > current_key:
            raise ValueError("IGNORED_STALE must use a key not greater than the current key")
    elif result == ShipmentApplicationResult.IGNORED_INVALID_TRANSITION.value:
        if target is current or not is_posterior:
            raise ValueError(
                "IGNORED_INVALID_TRANSITION must use a posterior key and a distinct state"
            )
    elif result == ShipmentApplicationResult.APPLIED.value:
        if target is current or not is_posterior:
            raise ValueError("APPLIED must use a posterior key and change the state")


def _validate_orders(dataset: LogicalDataset) -> None:
    by_order: dict[UUID, list[ShipmentRow]] = defaultdict(list)
    for shipment in dataset.shipments:
        by_order[shipment.order_id].append(shipment)
    for order in dataset.orders:
        shipments = by_order.get(order.id, [])
        if not shipments:
            raise ValueError("Order has no Shipment in the logical dataset")
        non_cancelled = [
            item for item in shipments if item.status != ShipmentStatus.CANCELLED.value
        ]
        should_be_fulfilled = bool(non_cancelled) and all(
            item.status == ShipmentStatus.DELIVERED.value for item in non_cancelled
        )
        if (order.status == "FULFILLED") != should_be_fulfilled:
            raise ValueError("Order FULFILLED status diverges from its Shipment states")


def _cohort_text(item: object, key: str) -> str:
    if not isinstance(item, dict) or not isinstance(item.get(key), str):
        raise ValueError("frozen cohort row is invalid")
    return cast(str, item[key])


def _normalize_slot(value: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", value.casefold())
