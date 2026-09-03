"""Pure frozen benchmark replay with no application or database dependency."""

from __future__ import annotations

import base64
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_CARRIERS = {
    "00000000-0000-4000-8000-000000000100": ("carrier-alpha", "alpha"),
    "00000000-0000-4000-8000-000000000101": ("carrier-beta", "beta"),
}
_ALPHA_STATUS = {
    "CREATED": "POSTED",
    "MOVING": "IN_TRANSIT",
    "OUT_FOR_DELIVERY": "OUT_FOR_DELIVERY",
    "DELIVERED": "DELIVERED",
    "PROBLEM": "EXCEPTION",
    "RETURNED": "RETURNED",
}
_BETA_STATUS = {
    "label_created": "POSTED",
    "hub_scan": "IN_TRANSIT",
    "courier_route": "OUT_FOR_DELIVERY",
    "completed": "DELIVERED",
    "delivery_issue": "EXCEPTION",
    "returned_origin": "RETURNED",
}
_ALLOWED = {
    "PENDING": frozenset(
        {"POSTED", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION", "CANCELLED"}
    ),
    "POSTED": frozenset({"IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION", "RETURNED"}),
    "IN_TRANSIT": frozenset({"OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION", "RETURNED"}),
    "OUT_FOR_DELIVERY": frozenset({"IN_TRANSIT", "DELIVERED", "EXCEPTION", "RETURNED"}),
    "EXCEPTION": frozenset({"IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "RETURNED"}),
    "DELIVERED": frozenset(),
    "RETURNED": frozenset(),
    "CANCELLED": frozenset(),
}
_SHIPPED = frozenset({"POSTED", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "RETURNED"})
_RESULTS = frozenset({"APPLIED", "NO_STATE_CHANGE", "IGNORED_STALE", "IGNORED_INVALID_TRANSITION"})


class SemanticValidationError(ValueError):
    """Stable semantic refusal that never embeds external payload content."""


@dataclass(frozen=True, slots=True)
class FrozenTransition:
    result: str
    previous_status: str
    resulting_status: str


@dataclass(slots=True)
class FrozenShipmentState:
    status: str
    status_occurred_at: datetime
    status_event_received_at: datetime | None
    status_external_event_id: str | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FrozenNormalizedEvent:
    external_event_id: str
    tracking_code: str
    external_status: str
    canonical_status: str
    occurred_at: datetime
    description: str | None
    location: str | None


def apply_frozen_status(
    state: FrozenShipmentState,
    target: str,
    *,
    occurred_at: datetime,
    received_at: datetime,
    external_event_id: str,
) -> FrozenTransition:
    """Apply the v1.0 Shipment transition contract without importing application code."""
    if state.status not in _ALLOWED or target not in _ALLOWED:
        raise SemanticValidationError("Shipment timeline contains an unknown state")
    if not external_event_id or not external_event_id.strip() or len(external_event_id) > 128:
        raise SemanticValidationError("Shipment timeline contains an invalid external event ID")
    previous = state.status
    incoming_key = (occurred_at, received_at, external_event_id)
    current_key = (
        None
        if state.status_event_received_at is None or state.status_external_event_id is None
        else (
            state.status_occurred_at,
            state.status_event_received_at,
            state.status_external_event_id,
        )
    )
    if current_key is not None and incoming_key <= current_key:
        return FrozenTransition("IGNORED_STALE", previous, previous)
    if target == previous:
        _advance_ordering(state, occurred_at, received_at, external_event_id)
        state.updated_at = received_at
        return FrozenTransition("NO_STATE_CHANGE", previous, previous)
    if target not in _ALLOWED[previous]:
        return FrozenTransition("IGNORED_INVALID_TRANSITION", previous, previous)
    state.status = target
    _advance_ordering(state, occurred_at, received_at, external_event_id)
    if state.shipped_at is None and target in _SHIPPED:
        state.shipped_at = occurred_at
    if target == "DELIVERED":
        state.delivered_at = occurred_at
    state.updated_at = received_at
    return FrozenTransition("APPLIED", previous, target)


def validate_semantic_document(document: Mapping[str, object]) -> None:
    """Replay every authenticated table row through the frozen pure v1.0 contract."""
    tables = _mapping(document, "tables")
    orders = _rows(tables, "orders")
    shipments = _rows(tables, "shipments")
    inboxes = _rows(tables, "carrier_event_inbox")
    events = _rows(tables, "tracking_events")
    notifications = _rows(tables, "notifications")

    order_by_id = _unique_map(orders, "Order")
    shipment_by_id = _unique_map(shipments, "Shipment")
    inbox_by_id = _unique_map(inboxes, "inbox")
    event_by_id = _unique_map(events, "TrackingEvent")

    for shipment in shipments:
        if str(shipment.get("order_id")) not in order_by_id:
            raise SemanticValidationError("Shipment references an unknown Order")
        carrier = _CARRIERS.get(str(shipment.get("carrier_id")))
        if carrier is None or shipment.get("carrier_code") != carrier[0]:
            raise SemanticValidationError("Shipment carrier identity is inconsistent")

    processed_ids: set[str] = set()
    for inbox in inboxes:
        carrier = _CARRIERS.get(str(inbox.get("carrier_id")))
        if carrier is None:
            raise SemanticValidationError("inbox references an unknown Carrier")
        raw_body = _raw_body(inbox)
        if hashlib.sha256(raw_body).hexdigest() != inbox.get("payload_sha256"):
            raise SemanticValidationError("inbox payload hash diverges from its raw body")
        status = inbox.get("status")
        if status == "PROCESSED":
            if (
                not isinstance(inbox.get("parsed_payload"), Mapping)
                or inbox.get("processed_at") is None
            ):
                raise SemanticValidationError("PROCESSED inbox is incomplete")
            processed_ids.add(str(inbox["id"]))
        elif status == "REJECTED":
            if not inbox.get("error_code") or inbox.get("processed_at") is None:
                raise SemanticValidationError("REJECTED inbox is incomplete")
        elif status == "RECEIVED":
            if inbox.get("processed_at") is not None or inbox.get("error_code") is not None:
                raise SemanticValidationError("RECEIVED inbox is already finalized")
        else:
            raise SemanticValidationError("inbox contains an unsupported status")

    events_by_inbox = Counter(str(event.get("inbox_event_id")) for event in events)
    if set(events_by_inbox) != processed_ids or set(events_by_inbox.values()) != {1}:
        raise SemanticValidationError("every PROCESSED inbox must own one TrackingEvent")

    events_by_shipment: dict[str, list[dict[str, object]]] = defaultdict(list)
    for event in events:
        event_inbox = inbox_by_id.get(str(event.get("inbox_event_id")))
        event_shipment = shipment_by_id.get(str(event.get("shipment_id")))
        if event_inbox is None or event_shipment is None:
            raise SemanticValidationError("TrackingEvent has a broken logical foreign key")
        if event.get("carrier_id") != event_inbox.get("carrier_id") or event.get(
            "carrier_id"
        ) != event_shipment.get("carrier_id"):
            raise SemanticValidationError("TrackingEvent carrier is inconsistent")
        normalized = normalize_frozen_payload(
            _CARRIERS[str(event["carrier_id"])][1], event_inbox.get("parsed_payload")
        )
        if (
            normalized.external_event_id != event_inbox.get("external_event_id")
            or normalized.tracking_code != event_shipment.get("tracking_code")
            or normalized.external_status != event.get("external_status")
            or normalized.canonical_status != event.get("canonical_status")
            or _iso(normalized.occurred_at) != event.get("occurred_at")
            or normalized.description != event.get("description")
            or normalized.location != event.get("location")
            or event.get("received_at") != event_inbox.get("received_at")
        ):
            raise SemanticValidationError("TrackingEvent diverges from its normalized payload")
        if event.get("application_result") not in _RESULTS:
            raise SemanticValidationError("TrackingEvent contains an unknown result")
        events_by_shipment[str(event_shipment["id"])].append(event)

    notifications_by_event = Counter(str(item.get("tracking_event_id")) for item in notifications)
    applied_ids = {
        str(item["id"]) for item in events if item.get("application_result") == "APPLIED"
    }
    if set(notifications_by_event) != applied_ids or set(notifications_by_event.values()) != {1}:
        raise SemanticValidationError("Notifications must correspond one-to-one to APPLIED events")
    for notification in notifications:
        notification_event = event_by_id.get(str(notification.get("tracking_event_id")))
        if notification_event is None or notification_event.get("application_result") != "APPLIED":
            raise SemanticValidationError("Notification references a non-APPLIED event")
        if notification.get("shipment_id") != notification_event.get("shipment_id"):
            raise SemanticValidationError("Notification Shipment does not match its event")
        if not str(notification.get("recipient", "")).endswith("@example.test"):
            raise SemanticValidationError("Notification recipient is not synthetic")

    for shipment in shipments:
        _replay_shipment(shipment, events_by_shipment.get(str(shipment["id"]), []), inbox_by_id)
    _validate_orders(orders, shipments)


def normalize_frozen_payload(adapter_key: str, payload: object) -> FrozenNormalizedEvent:
    """Normalize exactly the two frozen public carrier payload shapes."""
    if not isinstance(payload, Mapping):
        raise SemanticValidationError("processed carrier payload is not an object")
    try:
        if adapter_key == "alpha":
            external_status = _text(payload, "status")
            canonical = _ALPHA_STATUS[external_status]
            return FrozenNormalizedEvent(
                _text(payload, "eventId"),
                _text(payload, "trackingCode"),
                external_status,
                canonical,
                _parse_datetime(_text(payload, "eventDate")),
                _optional_text(payload.get("description")),
                _optional_text(payload.get("city")),
            )
        if adapter_key == "beta":
            event = payload.get("event")
            if not isinstance(event, Mapping):
                raise SemanticValidationError("Beta payload lacks its event object")
            external_status = _text(event, "type")
            canonical = _BETA_STATUS[external_status]
            location = payload.get("location")
            location_text: str | None = None
            if location is not None:
                if not isinstance(location, Mapping):
                    raise SemanticValidationError("Beta payload location is invalid")
                parts = [
                    item
                    for item in (
                        _optional_text(location.get("city")),
                        _optional_text(location.get("state")),
                    )
                    if item is not None
                ]
                location_text = ", ".join(parts) or None
            return FrozenNormalizedEvent(
                _text(payload, "id"),
                _text(payload, "tracking_number"),
                external_status,
                canonical,
                _parse_datetime(_text(event, "occurred_at")),
                _optional_text(event.get("details")),
                location_text,
            )
    except KeyError as exc:
        raise SemanticValidationError(
            "carrier payload contains an unknown external status"
        ) from exc
    raise SemanticValidationError("carrier payload uses an unsupported adapter")


def _replay_shipment(
    shipment: Mapping[str, object],
    events: list[dict[str, object]],
    inboxes: Mapping[str, dict[str, object]],
) -> None:
    created_at = _parse_datetime_value(shipment.get("created_at"))
    final_status = str(shipment.get("status"))
    cancelled = final_status == "CANCELLED"
    initial_occurred_at = (
        _parse_datetime_value(shipment.get("status_occurred_at")) if cancelled else created_at
    )
    state = FrozenShipmentState(
        "CANCELLED" if cancelled else "PENDING",
        initial_occurred_at,
        None,
        None,
        None,
        None,
        initial_occurred_at,
    )
    ordered = sorted(
        events,
        key=lambda item: (
            _parse_datetime_value(item.get("created_at")),
            _parse_datetime_value(item.get("received_at")),
            str(inboxes[str(item["inbox_event_id"])].get("external_event_id")),
        ),
    )
    for event in ordered:
        inbox = inboxes[str(event["inbox_event_id"])]
        transition = apply_frozen_status(
            state,
            str(event.get("canonical_status")),
            occurred_at=_parse_datetime_value(event.get("occurred_at")),
            received_at=_parse_datetime_value(event.get("received_at")),
            external_event_id=str(inbox.get("external_event_id")),
        )
        if (
            transition.result != event.get("application_result")
            or transition.previous_status != event.get("previous_shipment_status")
            or transition.resulting_status != event.get("resulting_shipment_status")
        ):
            raise SemanticValidationError("TrackingEvent does not match frozen Shipment replay")
    observed = (
        state.status,
        _iso(state.status_occurred_at),
        _optional_iso(state.status_event_received_at),
        state.status_external_event_id,
        _optional_iso(state.shipped_at),
        _optional_iso(state.delivered_at),
        _iso(state.updated_at),
    )
    expected = (
        shipment.get("status"),
        shipment.get("status_occurred_at"),
        shipment.get("status_event_received_at"),
        shipment.get("status_external_event_id"),
        shipment.get("shipped_at"),
        shipment.get("delivered_at"),
        shipment.get("updated_at"),
    )
    if observed != expected:
        raise SemanticValidationError("Shipment final state diverges from frozen timeline replay")


def _validate_orders(orders: list[dict[str, object]], shipments: list[dict[str, object]]) -> None:
    by_order: dict[str, list[dict[str, object]]] = defaultdict(list)
    for shipment in shipments:
        by_order[str(shipment.get("order_id"))].append(shipment)
    for order in orders:
        owned = by_order.get(str(order.get("id")), [])
        if not owned:
            raise SemanticValidationError("Order has no Shipment")
        non_cancelled = [item for item in owned if item.get("status") != "CANCELLED"]
        fulfilled = bool(non_cancelled) and all(
            item.get("status") == "DELIVERED" for item in non_cancelled
        )
        if (order.get("status") == "FULFILLED") != fulfilled:
            raise SemanticValidationError("Order status diverges from its Shipments")


def _raw_body(inbox: Mapping[str, object]) -> bytes:
    encoded = inbox.get("raw_body")
    if not isinstance(encoded, Mapping) or not isinstance(encoded.get("base64"), str):
        raise SemanticValidationError("inbox raw body encoding is invalid")
    try:
        raw_body = base64.b64decode(encoded["base64"], validate=True)
        parsed = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticValidationError("inbox raw body is not valid authenticated JSON") from exc
    projected = inbox.get("parsed_payload")
    if projected is not None and parsed != projected:
        raise SemanticValidationError("inbox parsed payload diverges from its raw body")
    return raw_body


def _advance_ordering(
    state: FrozenShipmentState,
    occurred_at: datetime,
    received_at: datetime,
    external_event_id: str,
) -> None:
    state.status_occurred_at = occurred_at
    state.status_event_received_at = received_at
    state.status_external_event_id = external_event_id


def _mapping(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise SemanticValidationError(f"artifact lacks its {key} object")
    return value


def _rows(tables: Mapping[str, object], key: str) -> list[dict[str, object]]:
    value = tables.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SemanticValidationError(f"artifact table {key} is invalid")
    return value


def _unique_map(rows: list[dict[str, object]], label: str) -> dict[str, dict[str, object]]:
    result = {str(row.get("id")): row for row in rows}
    if len(result) != len(rows) or "None" in result:
        raise SemanticValidationError(f"{label} identities are not unique")
    return result


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise SemanticValidationError("carrier payload contains an invalid text field")
    return item


def _optional_text(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise SemanticValidationError("carrier payload contains an invalid optional text field")


def _parse_datetime_value(value: object) -> datetime:
    if not isinstance(value, str):
        raise SemanticValidationError("artifact timestamp is not text")
    return _parse_datetime(value)


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SemanticValidationError("artifact timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise SemanticValidationError("artifact timestamp lacks timezone")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)
