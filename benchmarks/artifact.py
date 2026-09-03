"""Pure authenticated benchmark artifact contract with no application imports."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import cast

DATASET_SCHEMA_VERSION = 1
BENCHMARK_MANIFEST_NAME = "benchmark-v1.0.json"
BENCHMARK_HASH_NAME = "benchmark-v1.0.logical.sha256"


def canonical_json_bytes(value: object) -> bytes:
    """Serialize the one platform-independent JSON representation used on disk."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_authenticated_document(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[dict[str, object], str]:
    """Authenticate exact canonical bytes before exposing cohorts to an HTTP loadgen."""
    if path.name != BENCHMARK_MANIFEST_NAME:
        raise ValueError(f"campaign dataset must be the frozen {BENCHMARK_MANIFEST_NAME} artifact")
    persisted = path.read_bytes()
    try:
        parsed = json.loads(persisted.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("frozen benchmark artifact is not valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("frozen benchmark artifact must be a JSON object")
    document = cast(dict[str, object], parsed)
    if canonical_json_bytes(document) != persisted:
        raise ValueError("frozen benchmark artifact bytes are not canonical")
    actual = hashlib.sha256(persisted).hexdigest()
    sidecar = path.with_name(BENCHMARK_HASH_NAME)
    try:
        recorded = sidecar.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError("frozen benchmark hash sidecar is missing or invalid") from exc
    if recorded != actual:
        raise ValueError("frozen benchmark artifact does not match its hash sidecar")
    if expected_sha256 != actual:
        raise ValueError("campaign dataset digest does not match the authenticated artifact")
    metadata = document.get("metadata")
    tables = document.get("tables")
    cohorts = document.get("cohorts")
    if not isinstance(metadata, dict) or metadata.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise ValueError("unsupported benchmark dataset schema version")
    if metadata.get("dataset") != "benchmark":
        raise ValueError("campaign artifact is not the frozen benchmark dataset")
    if not isinstance(tables, dict) or not isinstance(cohorts, dict):
        raise ValueError("frozen benchmark artifact lacks tables or cohorts")
    for name in (
        "orders",
        "shipments",
        "carrier_event_inbox",
        "tracking_events",
        "notifications",
    ):
        rows = tables.get(name)
        if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
            raise ValueError(f"frozen benchmark table {name} is invalid")
        if rows != sorted(rows, key=lambda item: str(item.get("id", ""))):
            raise ValueError(f"frozen benchmark table {name} is not canonically ordered")
    _validate_relations(metadata, tables)
    return document, actual


def _validate_relations(metadata: dict[str, object], tables: dict[str, object]) -> None:
    rows = {
        name: cast(list[dict[str, object]], tables[name])
        for name in (
            "orders",
            "shipments",
            "carrier_event_inbox",
            "tracking_events",
            "notifications",
        )
    }
    counts = metadata.get("counts")
    actual_counts = {
        "orders": len(rows["orders"]),
        "shipments": len(rows["shipments"]),
        "carrier_event_inbox": len(rows["carrier_event_inbox"]),
        "tracking_events": len(rows["tracking_events"]),
        "notifications": len(rows["notifications"]),
    }
    if counts != actual_counts:
        raise ValueError("frozen benchmark metadata counts diverge from its complete tables")
    tracking_results = dict(
        sorted(
            Counter(str(item.get("application_result")) for item in rows["tracking_events"]).items()
        )
    )
    if metadata.get("tracking_results") != tracking_results:
        raise ValueError("frozen benchmark result matrix diverges from its TrackingEvents")
    all_ids = [str(item.get("id")) for table in rows.values() for item in table]
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("frozen benchmark tables contain a duplicate UUID identity")
    orders = {str(item["id"]): item for item in rows["orders"]}
    shipments = {str(item["id"]): item for item in rows["shipments"]}
    inboxes = {str(item["id"]): item for item in rows["carrier_event_inbox"]}
    events = {str(item["id"]): item for item in rows["tracking_events"]}
    if len({str(item["external_reference"]) for item in rows["orders"]}) != len(orders):
        raise ValueError("frozen benchmark contains duplicate Order references")
    if len({str(item["tracking_code"]) for item in rows["shipments"]}) != len(shipments):
        raise ValueError("frozen benchmark contains duplicate tracking codes")
    external_ids = [str(item["external_event_id"]) for item in rows["carrier_event_inbox"]]
    if len(set(external_ids)) != len(external_ids):
        raise ValueError("frozen benchmark contains duplicate external event IDs")
    if any(str(item["order_id"]) not in orders for item in rows["shipments"]):
        raise ValueError("frozen benchmark Shipment has an unknown Order")
    events_by_inbox: dict[str, list[dict[str, object]]] = {}
    for event in rows["tracking_events"]:
        inbox_id = str(event["inbox_event_id"])
        if inbox_id not in inboxes or str(event["shipment_id"]) not in shipments:
            raise ValueError("frozen benchmark TrackingEvent has a broken foreign key")
        events_by_inbox.setdefault(inbox_id, []).append(event)
    processed = {
        identifier for identifier, inbox in inboxes.items() if inbox.get("status") == "PROCESSED"
    }
    if set(events_by_inbox) != processed or any(
        len(owned) != 1 for owned in events_by_inbox.values()
    ):
        raise ValueError("frozen benchmark must have one TrackingEvent per PROCESSED inbox")
    notifications_by_event: dict[str, list[dict[str, object]]] = {}
    for notification in rows["notifications"]:
        event_id = str(notification["tracking_event_id"])
        referenced_event = events.get(event_id)
        if referenced_event is None or referenced_event.get("application_result") != "APPLIED":
            raise ValueError("frozen benchmark Notification references a non-APPLIED event")
        if notification.get("shipment_id") != referenced_event.get("shipment_id"):
            raise ValueError("frozen benchmark Notification Shipment does not match its event")
        notifications_by_event.setdefault(event_id, []).append(notification)
    applied = {
        identifier
        for identifier, event in events.items()
        if event.get("application_result") == "APPLIED"
    }
    if set(notifications_by_event) != applied or any(
        len(owned) != 1 for owned in notifications_by_event.values()
    ):
        raise ValueError("frozen benchmark requires one Notification for every APPLIED event")
    shipments_by_order: dict[str, list[dict[str, object]]] = {}
    for shipment in rows["shipments"]:
        shipments_by_order.setdefault(str(shipment["order_id"]), []).append(shipment)
    for identifier, order in orders.items():
        owned = shipments_by_order.get(identifier, [])
        non_cancelled = [item for item in owned if item.get("status") != "CANCELLED"]
        fulfilled = bool(non_cancelled) and all(
            item.get("status") == "DELIVERED" for item in non_cancelled
        )
        if (order.get("status") == "FULFILLED") != fulfilled:
            raise ValueError("frozen benchmark Order status diverges from its Shipments")
