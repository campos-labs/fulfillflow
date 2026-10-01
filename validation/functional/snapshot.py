"""Read only event-scoped assertions, never business data mutation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.db import Database

MESSAGE_FIELDS = (
    "message_id,type,event_id,correlation_id,body_sha256,state,attempts,generation,"
    "created_at,finished_at,last_attempt_at,reason"
)


def normalized(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, sort_keys=True))


async def rows(session: AsyncSession, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    result = await session.execute(text(query), params)
    return cast(list[dict[str, Any]], normalized([dict(row) for row in result.mappings()]))


async def local_snapshot(session: AsyncSession, owner: str, event_id: str) -> dict[str, Any]:
    params = {"event": event_id}
    data: dict[str, Any] = {}
    for table in ("message_inbox", "message_outbox"):
        data[table] = await rows(
            session, f"SELECT {MESSAGE_FIELDS} FROM {table} WHERE event_id=:event", params
        )
    # Each scenario owns otherwise empty service DBs. Counts avoid exporting
    # quarantined payloads while detecting an unexpected recovery intervention.
    for table in ("message_quarantine", "message_rearm"):
        data[table] = await session.scalar(text(f"SELECT count(*) FROM {table}"))
    if owner == "core":
        data["receipts"] = await rows(
            session,
            "SELECT event_id,content_sha256,result,created_at FROM tracking_event_receipts "
            "WHERE event_id=:event",
            params,
        )
        data["notifications"] = await rows(
            session,
            "SELECT id,tracking_event_id,shipment_id,status,created_at,simulated_at "
            "FROM notifications WHERE tracking_event_id=:event",
            params,
        )
    else:
        data["timeline"] = await rows(
            session,
            "SELECT id,inbox_event_id,received_at,created_at,application_result,"
            "resulting_shipment_status FROM tracking_events WHERE id=:event",
            params,
        )
    return data


async def snapshot(
    core: Database, tracking: Database, event_id: str, inbox_id: str, shipment_id: str
) -> dict[str, Any]:
    async with core.session() as session:
        core_data = await local_snapshot(session, "core", event_id)
        core_data["shipment"] = await rows(
            session, "SELECT id,order_id,status FROM shipments WHERE id=:id", {"id": shipment_id}
        )
        core_data["order"] = await rows(
            session,
            "SELECT id,status FROM orders WHERE id=:id",
            {"id": core_data["shipment"][0]["order_id"]},
        )
    async with tracking.session() as session:
        tracking_data = await local_snapshot(session, "tracking", event_id)
        inbox = await session.execute(
            text(
                "SELECT id,payload_sha256,raw_body,command,received_at,request_id,status,"
                "result,completed_at FROM carrier_event_inbox WHERE id=:id"
            ),
            {"id": inbox_id},
        )
        inbox_data = dict(inbox.mappings().one())
        inbox_data["raw_sha256"] = hashlib.sha256(bytes(inbox_data.pop("raw_body"))).hexdigest()
        inbox_data["command_sha256_observed"] = hashlib.sha256(
            json.dumps(inbox_data.pop("command"), sort_keys=True, default=str).encode()
        ).hexdigest()
        tracking_data["business_inbox"] = normalized(inbox_data)
    return {"core": core_data, "tracking": tracking_data}


def require(condition: bool, code: str) -> None:
    if not condition:
        raise AssertionError(code)


def check_pending(data: dict[str, Any], owner: str) -> None:
    require(len(data[owner]["message_inbox"]) == 1, "PENDING_INBOX_COUNT")
    require(data[owner]["message_inbox"][0]["state"] == "PENDING", "INBOX_NOT_PENDING")
    require(data["tracking"]["business_inbox"]["status"] == "RECEIVED", "BUSINESS_NOT_PENDING")
    require(not data["tracking"]["timeline"], "EARLY_TIMELINE")
    for service in ("core", "tracking"):
        require(data[service]["message_quarantine"] == 0, "UNEXPECTED_QUARANTINE")
        require(data[service]["message_rearm"] == 0, "UNEXPECTED_REARM")
    if owner == "core":
        require(not data["core"]["receipts"], "EARLY_RECEIPT")
        require(not data["core"]["notifications"], "EARLY_NOTIFICATION")
        require(not data["core"]["message_outbox"], "EARLY_RESULT")
        require(data["core"]["shipment"][0]["status"] == "PENDING", "EARLY_SHIPMENT")
        require(data["core"]["order"][0]["status"] == "CONFIRMED", "EARLY_ORDER")
    else:
        require(len(data["core"]["receipts"]) == 1, "CORE_RECEIPT_MISSING")
        require(len(data["core"]["notifications"]) == 1, "CORE_NOTIFICATION_MISSING")
        require(data["core"]["order"][0]["status"] == "FULFILLED", "CORE_NOT_COMPLETE")


def complete(data: dict[str, Any]) -> bool:
    return bool(
        data["tracking"]["business_inbox"]["status"] == "PROCESSED"
        and all(
            len(data[owner][table]) == 1 and data[owner][table][0]["state"] == state
            for owner in ("core", "tracking")
            for table, state in (("message_inbox", "DONE"), ("message_outbox", "SENT"))
        )
    )


def check_complete(data: dict[str, Any], initial: dict[str, Any]) -> None:
    require(complete(data), "FLOW_NOT_COMPLETE")
    require(len(data["core"]["receipts"]) == 1, "RECEIPT_COUNT")
    require(len(data["core"]["notifications"]) == 1, "NOTIFICATION_COUNT")
    require(data["core"]["notifications"][0]["status"] == "SIMULATED", "NOT_SIMULATED")
    require(len(data["tracking"]["timeline"]) == 1, "TIMELINE_COUNT")
    require(data["core"]["shipment"][0]["status"] == "DELIVERED", "SHIPMENT_NOT_DELIVERED")
    require(data["core"]["order"][0]["status"] == "FULFILLED", "ORDER_NOT_FULFILLED")
    for service in ("core", "tracking"):
        require(data[service]["message_quarantine"] == 0, "UNEXPECTED_QUARANTINE")
        require(data[service]["message_rearm"] == 0, "UNEXPECTED_REARM")
    for key in (
        "id",
        "received_at",
        "request_id",
        "payload_sha256",
        "raw_sha256",
        "command_sha256_observed",
    ):
        require(
            data["tracking"]["business_inbox"][key] == initial["tracking"]["business_inbox"][key],
            f"DURABLE_{key.upper()}_CHANGED",
        )
    durable_message_fields = (
        "message_id",
        "type",
        "event_id",
        "correlation_id",
        "body_sha256",
        "created_at",
    )
    for service in ("core", "tracking"):
        for table in ("message_inbox", "message_outbox"):
            old_rows = initial[service][table]
            if old_rows:
                require(len(old_rows) == 1, "INITIAL_MESSAGE_COUNT")
                for key in durable_message_fields:
                    require(
                        data[service][table][0][key] == old_rows[0][key],
                        f"DURABLE_MESSAGE_{key.upper()}_CHANGED",
                    )
    for sender, receiver in (("tracking", "core"), ("core", "tracking")):
        published = data[sender]["message_outbox"][0]
        received = data[receiver]["message_inbox"][0]
        require(
            all(published[k] == received[k] for k in durable_message_fields if k != "created_at"),
            "TRANSPORT_IDENTITY_MISMATCH",
        )
        require(
            published["correlation_id"] == data["tracking"]["business_inbox"]["id"],
            "CORRELATION_MISMATCH",
        )
    result = data["core"]["receipts"][0]["result"]
    require(result == data["tracking"]["business_inbox"]["result"], "RESULT_MISMATCH")
    timeline = data["tracking"]["timeline"][0]
    require(timeline["id"] == data["core"]["receipts"][0]["event_id"], "EVENT_ID_MISMATCH")
    require(result["event_id"] == timeline["id"], "RESULT_EVENT_ID_MISMATCH")
    require(
        timeline["inbox_event_id"] == data["tracking"]["business_inbox"]["id"],
        "TIMELINE_INBOX_MISMATCH",
    )
    notification = data["core"]["notifications"][0]
    require(notification["tracking_event_id"] == timeline["id"], "NOTIFICATION_EVENT_MISMATCH")
    require(
        notification["shipment_id"] == data["core"]["shipment"][0]["id"],
        "NOTIFICATION_SHIPMENT_MISMATCH",
    )
    require(result["shipment_id"] == data["core"]["shipment"][0]["id"], "RESULT_SHIPMENT_MISMATCH")
    require(
        datetime.fromisoformat(timeline["created_at"])
        == datetime.fromisoformat(result["decided_at"]),
        "DECISION_TIMESTAMP_MISMATCH",
    )
    require(
        datetime.fromisoformat(timeline["received_at"])
        == datetime.fromisoformat(data["tracking"]["business_inbox"]["received_at"]),
        "RECEIPT_TIMESTAMP_MISMATCH",
    )
    require(timeline["application_result"] == "APPLIED", "NOT_APPLIED")
    if initial["core"]["receipts"]:
        require(data["core"] == initial["core"], "CONFIRMED_CORE_CHANGED")
