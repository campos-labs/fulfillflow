"""Finalize Tracking from a validated Core decision in one local transaction."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.core import AppliedEventResult, ApplyEventCommand
from fulfillflow.contracts.messages import (
    CommandEnvelope,
    MessageEnvelope,
    ResultEnvelope,
    decode_message,
)
from fulfillflow.messaging.store import BlockedItemError
from fulfillflow.shared import Clock
from fulfillflow.tracking.domain import (
    InboxStatus,
    ShipmentApplicationResult,
    ShipmentStatus,
    TrackingEvent,
)
from fulfillflow.tracking.message_tables import tables
from fulfillflow.tracking.repository import TrackingRepository


async def apply_result(session: AsyncSession, message: MessageEnvelope, clock: Clock) -> None:
    if not isinstance(message, ResultEnvelope):
        raise BlockedItemError("WRONG_MESSAGE_FLOW")
    repository = TrackingRepository(session)
    inbox = await repository.get_inbox(message.correlation_id, for_update=True)
    if inbox is None or inbox.command is None:
        raise BlockedItemError("UNKNOWN_COMMAND")
    command = ApplyEventCommand.model_validate(inbox.command)
    body = await session.scalar(
        select(tables.outbox.c.body).where(tables.outbox.c.message_id == message.causation_id)
    )
    original = decode_message(body) if body is not None else None
    if (
        not isinstance(original, CommandEnvelope)
        or original.payload != command
        or message.event_id != command.event_id
        or message.correlation_id != original.correlation_id
        or message.request_id != original.request_id
        or message.payload.command_sha256 != command.content_hash()
    ):
        raise BlockedItemError("RESULT_COMMAND_MISMATCH")
    result = message.payload.result
    if inbox.status is not InboxStatus.RECEIVED:
        if inbox.result != result.model_dump(mode="json"):
            raise BlockedItemError("RESULT_CONFLICT")
        return
    if isinstance(result, AppliedEventResult):
        event = TrackingEvent(
            id=command.event_id,
            inbox_event_id=inbox.id,
            shipment_id=result.shipment_id,
            carrier_id=command.carrier_id,
            external_status=command.external_status,
            canonical_status=ShipmentStatus(command.canonical_status),
            description=command.description,
            location=command.location,
            occurred_at=command.occurred_at,
            received_at=command.received_at,
            application_result=ShipmentApplicationResult(result.result),
            previous_shipment_status=ShipmentStatus(result.previous_status),
            resulting_shipment_status=ShipmentStatus(result.current_status),
            created_at=result.decided_at,
        )
        await repository.add_tracking_event(event)
        inbox.mark_processed(clock.now())
    else:
        inbox.mark_rejected(
            code=result.code,
            detail=result.detail,
            processed_at=result.decided_at,
            parsed_payload=inbox.parsed_payload,
        )
    inbox.completed_at = clock.now()
    inbox.result = result.model_dump(mode="json")
    await repository.save_inbox(inbox)
