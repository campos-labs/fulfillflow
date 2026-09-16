"""Synthetic deterministic messages shared by transport tests."""

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from fulfillflow.contracts.core import ApplyEventCommand, RejectedEventResult
from fulfillflow.contracts.messages import (
    CommandEnvelope,
    ResultEnvelope,
    ResultPayload,
    canonical_bytes,
)

NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def command_message(number: int = 1) -> CommandEnvelope:
    payload = ApplyEventCommand(
        event_id=UUID(int=number),
        carrier_id=UUID(int=100),
        external_event_id=f"test-{number}",
        payload_sha256="a" * 64,
        tracking_code="TEST",
        canonical_status="POSTED",
        occurred_at=NOW,
        received_at=NOW,
        external_status="posted",
        description=None,
        location=None,
    )
    return CommandEnvelope(
        message_id=UUID(int=number + 1000),
        event_id=payload.event_id,
        correlation_id=UUID(int=number + 2000),
        causation_id=UUID(int=number + 2000),
        request_id=UUID(int=3000),
        created_at=NOW,
        payload=payload,
        payload_sha256=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
    )


def result_message(number: int = 1) -> ResultEnvelope:
    command = command_message(number)
    payload = ResultPayload(
        command_sha256=command.payload.content_hash(),
        result=RejectedEventResult(event_id=command.event_id, decided_at=NOW),
    )
    return ResultEnvelope(
        message_id=UUID(int=number + 4000),
        event_id=command.event_id,
        correlation_id=command.correlation_id,
        causation_id=command.message_id,
        request_id=command.request_id,
        created_at=NOW,
        payload=payload,
        payload_sha256=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
    )
