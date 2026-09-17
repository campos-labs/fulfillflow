"""Synthetic stable applied-transition messages for Notifications verification."""

import hashlib
from uuid import UUID

from fulfillflow.contracts.messages import (
    NotificationEnvelope,
    NotificationPayload,
    canonical_bytes,
)
from tests.message_support import NOW, command_message


def notification_message(number: int = 1) -> NotificationEnvelope:
    command = command_message(number)
    payload = NotificationPayload(
        shipment_id=UUID(int=number + 5000),
        resulting_status="POSTED",
        recipient="recipient@example.test",
    )
    return NotificationEnvelope(
        message_id=UUID(int=number + 6000),
        event_id=command.event_id,
        correlation_id=command.correlation_id,
        causation_id=command.message_id,
        request_id=command.request_id,
        created_at=NOW,
        payload=payload,
        payload_sha256=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
    )
