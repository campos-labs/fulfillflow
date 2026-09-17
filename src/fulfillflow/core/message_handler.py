"""Core's local command application and atomic result outbox."""

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.messages import (
    CommandEnvelope,
    MessageEnvelope,
    NotificationEnvelope,
    ResultEnvelope,
    ResultPayload,
    canonical_bytes,
    decode_message,
)
from fulfillflow.core.events import CoreEventService
from fulfillflow.core.message_tables import tables
from fulfillflow.messaging.store import BlockedItemError, put_message
from fulfillflow.shared import Clock, new_uuid


async def apply_command(session: AsyncSession, message: MessageEnvelope, clock: Clock) -> None:
    if not isinstance(message, CommandEnvelope):
        raise BlockedItemError("WRONG_MESSAGE_FLOW")
    application = await CoreEventService(session, clock).apply_in_transaction(message.payload)
    result = application.result
    if application.notification is not None:
        fact = NotificationEnvelope(
            message_id=new_uuid(),
            event_id=message.event_id,
            correlation_id=message.correlation_id,
            causation_id=message.message_id,
            request_id=message.request_id,
            created_at=result.decided_at,
            payload=application.notification,
            payload_sha256=hashlib.sha256(canonical_bytes(application.notification)).hexdigest(),
        )
        await put_message(session, tables.outbox, fact, clock.now())
    payload = ResultPayload(command_sha256=message.payload.content_hash(), result=result)
    existing_body = await session.scalar(
        select(tables.outbox.c.body).where(
            tables.outbox.c.type == "tracking.result.v1",
            tables.outbox.c.event_id == message.event_id,
        )
    )
    if existing_body is not None:
        existing = decode_message(existing_body)
        if (
            not isinstance(existing, ResultEnvelope)
            or existing.payload != payload
            or existing.causation_id != message.message_id
            or existing.correlation_id != message.correlation_id
            or existing.request_id != message.request_id
        ):
            raise BlockedItemError("RESULT_OUTBOX_CONFLICT")
        return
    envelope = ResultEnvelope(
        message_id=new_uuid(),
        event_id=message.event_id,
        correlation_id=message.correlation_id,
        causation_id=message.message_id,
        request_id=message.request_id,
        created_at=clock.now(),
        payload=payload,
        payload_sha256=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
    )
    await put_message(session, tables.outbox, envelope, clock.now())
