"""The new flow is explicit, minimal and independent of Tracking's result."""

import hashlib
import json
from datetime import timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError
from tests.message_support import NOW, command_message, result_message
from tests.notification_support import notification_message

from fulfillflow.contracts.messages import (
    NotificationEnvelope,
    NotificationPayload,
    canonical_bytes,
    decode_message,
    encode_message,
)
from fulfillflow.contracts.notifications import NotificationProgress, NotificationStatusView


@pytest.mark.parametrize("message", [command_message(), result_message(), notification_message()])
def test_all_flows_roundtrip_without_changing_original_envelopes(message):
    wire = encode_message(message)
    assert decode_message(wire) == message
    assert encode_message(decode_message(wire)) == wire


def test_notification_fact_is_minimal_and_time_is_canonical():
    event = notification_message()
    assert set(event.payload.model_dump()) == {"shipment_id", "resulting_status", "recipient"}
    assert event.causation_id == command_message().message_id
    assert event.event_id == command_message().event_id
    fields = event.model_dump()
    fields["created_at"] = NOW.astimezone(timezone(timedelta(hours=-3)))
    assert encode_message(NotificationEnvelope.model_validate(fields)) == encode_message(event)
    assert b"result" not in encode_message(event).replace(b"resulting_status", b"")


@pytest.mark.parametrize(
    "status", ["POSTED", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION", "RETURNED"]
)
def test_only_applied_tracking_targets_are_admitted(status):
    values = notification_message().payload.model_dump()
    values["resulting_status"] = status
    assert NotificationPayload.model_validate(values).resulting_status == status


@pytest.mark.parametrize(
    "field,value",
    [
        ("resulting_status", "PENDING"),
        ("resulting_status", "CANCELLED"),
        ("recipient", " recipient@example.test"),
        ("recipient", ""),
        ("recipient", " " * 4),
        ("recipient", "x" * 255),
        ("shipment_id", "bad-id"),
        ("raw_body", "must-not-pass"),
    ],
)
def test_rejects_noncanonical_or_unnecessary_payload(field, value):
    payload = notification_message().payload.model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        NotificationPayload.model_validate(payload)


def test_corrupted_snapshot_hash_and_naive_time_are_rejected():
    fields = notification_message().model_dump(mode="json")
    fields["payload"]["recipient"] = "different@example.test"
    with pytest.raises(ValidationError, match="payload hash"):
        decode_message(json.dumps(fields).encode())
    fields = notification_message().model_dump(mode="json")
    fields["created_at"] = "2026-09-15T12:00:00"
    with pytest.raises(ValidationError):
        decode_message(json.dumps(fields).encode())
    event = notification_message()
    assert event.payload_sha256 == hashlib.sha256(canonical_bytes(event.payload)).hexdigest()


def test_nonrequired_and_not_received_are_distinct_observations():
    result = NotificationStatusView(
        tracking_event_id=UUID(int=1), required=False, core_observed_at=NOW
    )
    assert result.origin is result.processing is result.notifications_observed_at is None
    pending = NotificationStatusView(
        tracking_event_id=UUID(int=1),
        required=True,
        origin="ASYNC",
        publication="SENT",
        processing="NOT_RECEIVED",
        core_observed_at=NOW,
        notifications_observed_at=NOW,
    )
    assert pending.notification_id is None
    with pytest.raises(ValidationError):
        NotificationStatusView.model_validate(result.model_dump() | {"origin": "ASYNC"})


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "ASYNC", "processing": "DONE"},
        {"origin": "LEGACY", "processing": None},
        {"origin": None, "processing": "DONE"},
        {"origin": "ASYNC", "processing": "PENDING", "simulated_at": NOW},
    ],
)
def test_inconsistent_progress_cannot_be_presented_as_completion(changes):
    with pytest.raises(ValidationError):
        NotificationProgress(tracking_event_id=UUID(int=1), observed_at=NOW, **changes)


def test_legacy_requires_explicit_record_and_no_publication():
    result = NotificationStatusView(
        tracking_event_id=UUID(int=1),
        required=True,
        origin="LEGACY",
        notification_id=UUID(int=2),
        status="SIMULATED",
        simulated_at=NOW,
        core_observed_at=NOW,
        notifications_observed_at=NOW,
    )
    with pytest.raises(ValidationError):
        NotificationStatusView.model_validate(result.model_dump() | {"publication": "SENT"})
