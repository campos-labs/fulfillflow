"""RabbitMQ transport: SQL scopes end before publish or acknowledgement."""

from datetime import datetime

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from pamqp.commands import Basic

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
from fulfillflow.shared import Clock

FLOWS = ("tracking.apply.v1", "tracking.result.v1")


async def declare_flow(channel: AbstractChannel, flow: str) -> None:
    if flow not in FLOWS:
        raise ValueError("Unknown flow")
    exchange = await channel.declare_exchange(
        flow, aio_pika.ExchangeType.DIRECT, durable=True, auto_delete=False
    )
    queue = await channel.declare_queue(
        flow,
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
        raise RuntimeError("PUBLISH_NOT_CONFIRMED")


async def receive(
    database: Database,
    tables: MessageTables,
    incoming: AbstractIncomingMessage,
    flow: str,
    now: datetime,
) -> None:
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
                await quarantine(session, tables, incoming.body, "MESSAGE_IDENTITY_CONFLICT", now)
    await incoming.ack()


async def publish_batch(
    database: Database, tables: MessageTables, channel: AbstractChannel, clock: Clock
) -> int:
    async with database.session() as session, session.begin():
        items = await claim_publications(session, tables.outbox, clock.now())
    for item in items:
        try:
            await publish(channel, decode_message(item.body))
        except aio_pika.exceptions.DeliveryError:
            async with database.session() as session, session.begin():
                await retry_publication(session, tables.outbox, item, clock.now())
        else:
            async with database.session() as session, session.begin():
                await mark_sent(session, tables.outbox, item, clock.now())
    return len(items)
