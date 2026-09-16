"""RabbitMQ transport: SQL scopes end before publish or acknowledgement."""

import time
from datetime import datetime

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from pamqp.commands import Basic
from sqlalchemy.exc import InterfaceError, OperationalError

from fulfillflow.contracts.messages import MessageEnvelope, decode_message, encode_message
from fulfillflow.db import Database
from fulfillflow.messaging.store import (
    MessageConflictError,
    claim_publications,
    mark_sent,
    put_message,
    quarantine,
    retry_publication,
)
from fulfillflow.messaging.tables import MessageTables
from fulfillflow.messaging.telemetry import emit
from fulfillflow.shared import Clock

DEPENDENCY_ERRORS = (
    OSError,
    TimeoutError,
    aio_pika.exceptions.AMQPConnectionError,
    aio_pika.exceptions.ChannelInvalidStateError,
    OperationalError,
    InterfaceError,
)


class PublishNotConfirmedError(Exception):
    """A negative/absent confirm is never publication success."""


FLOWS = ("tracking.apply.v1", "tracking.result.v1")


async def declare_flow(channel: AbstractChannel, flow: str) -> None:
    if flow not in FLOWS:
        raise ValueError("Unknown flow")
    exchange = await channel.declare_exchange(
        flow, aio_pika.ExchangeType.DIRECT, durable=True, auto_delete=False
    )
    queue = await channel.declare_queue(
        f"{flow}.queue",
        durable=True,
        exclusive=False,
        auto_delete=False,
        arguments={"x-queue-type": "classic"},
    )
    await queue.bind(exchange, routing_key=flow)


async def publish(channel: AbstractChannel, envelope: MessageEnvelope) -> None:
    exchange = await channel.get_exchange(envelope.type, ensure=False)
    result = await exchange.publish(
        aio_pika.Message(
            body=encode_message(envelope),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
            content_encoding="utf-8",
            message_id=str(envelope.message_id),
            correlation_id=str(envelope.correlation_id),
            type=envelope.type,
            timestamp=envelope.created_at,
        ),
        routing_key=envelope.type,
        mandatory=True,
        timeout=5,
    )
    if not isinstance(result, Basic.Ack):
        raise PublishNotConfirmedError("PUBLISH_NOT_CONFIRMED")


async def receive(
    database: Database,
    tables: MessageTables,
    incoming: AbstractIncomingMessage,
    flow: str,
    now: datetime,
) -> None:
    started = time.monotonic()
    reason = None
    envelope = None
    try:
        envelope = decode_message(incoming.body)
        if (
            envelope.type != flow
            or incoming.type != flow
            or incoming.message_id != str(envelope.message_id)
            or incoming.correlation_id != str(envelope.correlation_id)
            or incoming.content_type != "application/json"
            or incoming.content_encoding != "utf-8"
            or incoming.delivery_mode != aio_pika.DeliveryMode.PERSISTENT
            or incoming.timestamp is None
            or int(incoming.timestamp.timestamp()) != int(envelope.created_at.timestamp())
        ):
            reason = "AMQP_PROPERTIES_MISMATCH"
    except ValueError:
        reason = "INVALID_ENVELOPE"
    # If commit fails, the caller closes the channel without ACK.
    async with database.session() as session, session.begin():
        if reason is not None or envelope is None:
            await quarantine(session, tables, incoming.body, reason or "INVALID_ENVELOPE", now)
        else:
            try:
                await put_message(session, tables.inbox, envelope, now)
            except MessageConflictError:
                reason = "MESSAGE_IDENTITY_CONFLICT"
                await quarantine(session, tables, incoming.body, "MESSAGE_IDENTITY_CONFLICT", now)
    await incoming.ack()
    emit(
        tables.owner,
        "receive",
        "QUARANTINED" if reason else "PERSISTED",
        category=reason,
        duration=time.monotonic() - started,
        message=envelope.model_dump() if envelope is not None else None,
    )


async def publish_batch(
    database: Database, tables: MessageTables, channel: AbstractChannel, clock: Clock
) -> int:
    async with database.session() as session, session.begin():
        items = await claim_publications(session, tables.outbox, clock.now())
    for item in items:
        started = time.monotonic()
        envelope = None
        category: str | None
        activity = dict(
            message_id=item.message_id, attempts=item.attempts, generation=item.generation
        )
        try:
            envelope = decode_message(item.body)
            activity.update(envelope.model_dump())
            await publish(channel, envelope)
        except DEPENDENCY_ERRORS:
            # Leave uncertain publication leased for redelivery; do not charge every item.
            raise
        except (aio_pika.exceptions.DeliveryError, PublishNotConfirmedError):
            async with database.session() as session, session.begin():
                state = await retry_publication(session, tables.outbox, item, clock.now())
            outcome, category = state or "STALE_LEASE", "PUBLISH_REJECTED"
        except Exception:
            async with database.session() as session, session.begin():
                state = await retry_publication(
                    session, tables.outbox, item, clock.now(), retryable=False
                )
            outcome, category = state or "STALE_LEASE", "UNEXPECTED_PUBLICATION_ERROR"
        else:
            async with database.session() as session, session.begin():
                sent = await mark_sent(session, tables.outbox, item, clock.now())
            outcome, category = ("SENT" if sent else "STALE_LEASE"), None
        service = tables.owner
        emit(
            service,
            "publish",
            outcome,
            category=category,
            message=activity,
            duration=time.monotonic() - started,
        )
    return len(items)
