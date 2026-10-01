"""Reject false recovery and identity changes, not just missing count assertions."""

import copy

import pytest
from validation.functional.snapshot import check_complete, require


def state():
    decision = {
        "event_id": "event",
        "shipment_id": "shipment",
        "decided_at": "2026-09-22T12:00:00Z",
    }
    business = dict(
        id="inbox",
        received_at="2026-09-22T11:59:00Z",
        request_id="request",
        payload_sha256="hash",
        raw_sha256="hash",
        command_sha256_observed="command",
        status="PROCESSED",
        result=copy.deepcopy(decision),
    )
    result = {
        "core": {
            "message_inbox": [{"state": "DONE"}],
            "message_outbox": [{"state": "SENT"}],
            "message_quarantine": 0,
            "message_rearm": 0,
            "receipts": [{"event_id": "event", "result": copy.deepcopy(decision)}],
            "notifications": [
                {"status": "SIMULATED", "tracking_event_id": "event", "shipment_id": "shipment"}
            ],
            "shipment": [{"id": "shipment", "status": "DELIVERED"}],
            "order": [{"status": "FULFILLED"}],
        },
        "tracking": {
            "message_inbox": [{"state": "DONE"}],
            "message_outbox": [{"state": "SENT"}],
            "message_quarantine": 0,
            "message_rearm": 0,
            "business_inbox": business,
            "timeline": [
                {
                    "id": "event",
                    "application_result": "APPLIED",
                    "inbox_event_id": "inbox",
                    "created_at": "2026-09-22 12:00:00+00:00",
                    "received_at": "2026-09-22 11:59:00+00:00",
                }
            ],
        },
    }
    for sender, receiver, flow in (
        ("tracking", "core", "tracking.apply.v1"),
        ("core", "tracking", "tracking.result.v1"),
    ):
        fields = dict(
            message_id=flow,
            type=flow,
            event_id="event",
            correlation_id="inbox",
            body_sha256=flow + "-hash",
            created_at="2026-09-22T12:00:00Z",
        )
        result[sender]["message_outbox"][0].update(fields)
        result[receiver]["message_inbox"][0].update(fields)
    initial = copy.deepcopy(result)
    initial["core"]["receipts"] = []
    return result, initial


def test_complete_business_state_passes():
    final, initial = state()
    check_complete(final, initial)


@pytest.mark.parametrize(
    "mutation",
    (
        "duplicate_effect",
        "wrong_result",
        "different_inbox",
        "broker_sent_but_not_done",
        "no_order_completion",
        "pending_publication",
        "failed_notification",
    ),
)
def test_rejects_incorrect_business_outcomes(mutation):
    final, initial = state()
    if mutation == "duplicate_effect":
        final["core"]["notifications"].append({"status": "SIMULATED"})
    elif mutation == "wrong_result":
        final["tracking"]["business_inbox"]["result"] = {"event_id": "other"}
    elif mutation == "different_inbox":
        final["tracking"]["business_inbox"]["id"] = "other"
    elif mutation == "broker_sent_but_not_done":
        final["tracking"]["message_inbox"][0]["state"] = "PENDING"
    elif mutation == "no_order_completion":
        final["core"]["order"][0]["status"] = "CONFIRMED"
    elif mutation == "pending_publication":
        final["core"]["message_outbox"][0]["state"] = "LEASED"
    else:
        final["core"]["notifications"][0]["status"] = "FAILED"
    with pytest.raises(AssertionError):
        check_complete(final, initial)


def test_confirmed_core_result_cannot_change_after_tracking_crash():
    final, _ = state()
    initial = copy.deepcopy(final)
    final["core"]["receipts"][0]["result"]["decided_at"] = "2026-09-22T12:00:01Z"
    final["tracking"]["business_inbox"]["result"]["decided_at"] = "2026-09-22T12:00:01Z"
    with pytest.raises(AssertionError):
        check_complete(final, initial)


@pytest.mark.parametrize("table", ["message_quarantine", "message_rearm"])
@pytest.mark.parametrize("owner", ["core", "tracking"])
def test_recovery_cannot_hide_quarantine_or_administrative_rearm(table, owner):
    final, initial = state()
    final[owner][table] = 1
    with pytest.raises(AssertionError, match="UNEXPECTED_"):
        check_complete(final, initial)


@pytest.mark.parametrize("field", ["inbox_event_id", "created_at", "received_at"])
def test_timeline_must_retain_original_identity_and_decision_times(field):
    final, initial = state()
    final["tracking"]["timeline"][0][field] = (
        "other" if field == "inbox_event_id" else "2026-09-22T12:00:30Z"
    )
    with pytest.raises(AssertionError):
        check_complete(final, initial)


def test_assertion_code_is_explicit():
    with pytest.raises(AssertionError, match="EXPECTED_CODE"):
        require(False, "EXPECTED_CODE")


@pytest.mark.parametrize("field", ["message_id", "body_sha256", "correlation_id", "created_at"])
def test_durable_transport_identity_cannot_change(field):
    final, initial = state()
    final["core"]["message_inbox"][0][field] = "changed"
    with pytest.raises(AssertionError, match="DURABLE_MESSAGE_"):
        check_complete(final, initial)


def test_new_transport_rows_must_match_published_identity():
    final, initial = state()
    initial["core"]["message_outbox"] = []
    initial["tracking"]["message_inbox"] = []
    final["tracking"]["message_inbox"][0]["body_sha256"] = "different"
    with pytest.raises(AssertionError, match="TRANSPORT_IDENTITY_MISMATCH"):
        check_complete(final, initial)


def test_local_receive_time_need_not_equal_publication_row_time():
    final, initial = state()
    final["core"]["message_inbox"][0]["created_at"] = "2026-09-22T12:00:01Z"
    initial["core"]["message_inbox"][0]["created_at"] = "2026-09-22T12:00:01Z"
    check_complete(final, initial)
