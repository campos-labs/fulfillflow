"""Real PostgreSQL/RabbitMQ boundaries and durable local recovery."""

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from uuid import UUID

import aio_pika
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.message_support import NOW, command_message, result_message
from tests.support import FixedClock

from fulfillflow.contracts.messages import MessageEnvelope
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging.amqp import declare_flow, publish, publish_batch, receive
from fulfillflow.messaging.store import (
    MessageConflictError,
    RetryableItemError,
    claim_publications,
    mark_sent,
    process_one,
    put_message,
)
from fulfillflow.tracking.message_tables import tables as tracking_tables


@pytest.fixture
async def owners(postgres_database, postgres_tracking_database):
    pairs = [(postgres_database, core_tables), (postgres_tracking_database, tracking_tables)]
    for database, tables in pairs:
        async with database.session() as session, session.begin():
            for table in (tables.inbox, tables.outbox, tables.quarantine):
                await session.execute(delete(table))
    yield pairs
    for database, tables in pairs:
        async with database.session() as session, session.begin():
            for table in (tables.inbox, tables.outbox, tables.quarantine):
                await session.execute(delete(table))


@pytest.fixture
async def channel():
    url = os.environ.get("TEST_AMQP_URL")
    assert url, "TEST_AMQP_URL must identify the isolated real RabbitMQ test vhost"
    connection = await aio_pika.connect(url, timeout=10)
    async with connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        await channel.set_qos(prefetch_count=8)
        for flow in ("tracking.apply.v1", "tracking.result.v1"):
            await declare_flow(channel, flow)
            queue = await channel.get_queue(flow)
            await queue.purge()
        yield channel


@pytest.mark.parametrize("direction", [0, 1])
async def test_durable_ack_restart_and_duplicate(owners, channel, direction):
    destination, tables = owners[direction]
    source, source_tables = owners[1 - direction]
    message = command_message() if direction == 0 else result_message()
    async with source.session() as session, session.begin():
        await put_message(session, source_tables.outbox, message, NOW)
    assert await publish_batch(source, source_tables, channel, FixedClock(NOW)) == 1
    queue = await channel.get_queue(message.type)
    incoming = await queue.get(timeout=5)
    assert incoming is not None
    await receive(destination, tables, incoming, message.type, NOW)
    assert await queue.get(fail=False, timeout=5) is None
    async with destination.session() as session:
        assert await session.scalar(select(tables.inbox.c.state)) == "PENDING"
    # Closing all local connections simulates a fresh local processor after ACK.
    await destination.dispose()

    async def apply(session: AsyncSession, envelope: MessageEnvelope) -> None:
        assert envelope == message
        await session.execute(text("SELECT 1"))

    environment = dict(
        os.environ,
        DATABASE_URL=destination.engine.url.render_as_string(hide_password=False),
        PROBE_OWNER="core" if direction == 0 else "tracking",
    )
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.message_recovery_probe"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    await publish(channel, message)
    duplicate = await queue.get(timeout=5)
    await receive(destination, tables, duplicate, message.type, NOW)
    async with destination.session() as session, session.begin():
        assert not await process_one(session, tables.inbox, NOW, apply)
        assert await session.scalar(select(func.count()).select_from(tables.inbox)) == 1
        assert await session.scalar(select(tables.inbox.c.state)) == "DONE"


async def test_lost_confirm_expired_lease_and_stale_owner(owners, channel):
    database, tables = owners[0]
    message = result_message()
    async with database.session() as session, session.begin():
        await put_message(session, tables.outbox, message, NOW)
        (first,) = await claim_publications(session, tables.outbox, NOW)
    await publish(channel, message)  # Confirm received but not recorded: process crash.
    later = NOW + timedelta(seconds=31)
    async with database.session() as session, session.begin():
        (second,) = await claim_publications(session, tables.outbox, later)
        assert first.token != second.token
        assert first.body == second.body
        await mark_sent(session, tables.outbox, first, later)
        assert await session.scalar(select(tables.outbox.c.state)) == "LEASED"
    await publish(channel, message)
    async with database.session() as session, session.begin():
        await mark_sent(session, tables.outbox, second, later)
        assert await session.scalar(select(tables.outbox.c.state)) == "SENT"


async def test_unroutable_is_not_confirmed(channel):
    flow = "tracking.apply.v1"
    queue = await channel.get_queue(flow)
    await queue.unbind(flow, routing_key=flow)
    try:
        with pytest.raises(aio_pika.exceptions.DeliveryError):
            await publish(channel, command_message())
    finally:
        await queue.bind(flow, routing_key=flow)


async def test_concurrent_unique_and_divergent_identity(owners):
    database, tables = owners[0]
    message = command_message()

    async def insert() -> None:
        async with database.session() as session, session.begin():
            await put_message(session, tables.inbox, message, NOW)

    async with asyncio.timeout(10):
        await asyncio.gather(insert(), insert())
    async with database.session() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(tables.inbox)) == 1
        changed = message.model_copy(update={"message_id": UUID(int=9000)})
        with pytest.raises(MessageConflictError):
            await put_message(session, tables.inbox, changed, NOW)


async def test_local_retry_savepoint_and_exhaustion(owners):
    database, tables = owners[0]
    async with database.session() as session, session.begin():
        await put_message(session, tables.inbox, command_message(), NOW)

    async def fail(session: AsyncSession, envelope: MessageEnvelope) -> None:
        await put_message(session, tables.outbox, result_message(), NOW)
        raise RetryableItemError

    when = NOW
    for attempt, wait in enumerate((1, 5, 15, 60, 60), 1):
        async with database.session() as session, session.begin():
            assert await process_one(session, tables.inbox, when, fail)
            assert await session.scalar(select(tables.inbox.c.attempts)) == attempt
            assert await session.scalar(select(func.count()).select_from(tables.outbox)) == 0
        when += timedelta(seconds=wait)
    async with database.session() as session, session.begin():
        assert await session.scalar(select(tables.inbox.c.state)) == "BLOCKED"
        assert not await process_one(session, tables.inbox, when, fail)


async def test_malformed_quarantine_before_ack(owners, channel):
    database, tables = owners[0]
    exchange = await channel.get_exchange("tracking.apply.v1")
    await exchange.publish(aio_pika.Message(body=b"not-json"), routing_key="tracking.apply.v1")
    queue = await channel.get_queue("tracking.apply.v1")
    incoming = await queue.get(timeout=5)
    await receive(database, tables, incoming, "tracking.apply.v1", NOW)
    async with database.session() as session:
        assert await session.scalar(select(tables.quarantine.c.reason)) == "INVALID_ENVELOPE"
        assert await session.scalar(select(func.count()).select_from(tables.inbox)) == 0
    assert await queue.get(fail=False) is None


async def test_commit_then_ack_failure_redelivers_without_duplicate(owners, channel, monkeypatch):
    database, tables = owners[0]
    message = command_message()
    await publish(channel, message)
    queue = await channel.get_queue(message.type)
    incoming = await queue.get(timeout=5)
    original_ack = aio_pika.IncomingMessage.ack

    async def lost_ack(self, multiple=False):
        raise ConnectionError("injected lost ACK")

    monkeypatch.setattr(aio_pika.IncomingMessage, "ack", lost_ack)
    with pytest.raises(ConnectionError):
        await receive(database, tables, incoming, message.type, NOW)
    async with database.session() as session:
        assert await session.scalar(select(tables.inbox.c.state)) == "PENDING"
    monkeypatch.setattr(aio_pika.IncomingMessage, "ack", original_ack)
    await incoming.reject(requeue=True)
    redelivery = await queue.get(timeout=5)
    assert redelivery.redelivered
    await receive(database, tables, redelivery, message.type, NOW)
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(tables.inbox)) == 1


async def test_failed_commit_never_acks(owners, channel, monkeypatch):
    database, tables = owners[0]
    message = command_message()
    await publish(channel, message)
    queue = await channel.get_queue(message.type)
    incoming = await queue.get(timeout=5)
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction

    original_commit = AsyncSessionTransaction.__aexit__

    async def uncertain(self, *args):
        await self.rollback()
        raise ConnectionError("injected commit uncertainty")

    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", uncertain)
    with pytest.raises(ConnectionError):
        await receive(database, tables, incoming, message.type, NOW)
    assert not incoming.processed
    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", original_commit)
    await incoming.reject(requeue=True)
    redelivery = await queue.get(timeout=5)
    await receive(database, tables, redelivery, message.type, NOW)
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(tables.inbox)) == 1


async def test_publication_has_no_checked_out_sql_connection(owners, channel, monkeypatch):
    database, tables = owners[0]
    async with database.session() as session, session.begin():
        await put_message(session, tables.outbox, result_message(), NOW)
    from fulfillflow.messaging import amqp

    original = amqp.publish

    async def checked(channel, envelope):
        assert database.engine.pool.checkedout() == 0
        await original(channel, envelope)

    monkeypatch.setattr(amqp, "publish", checked)
    assert await publish_batch(database, tables, channel, FixedClock(NOW)) == 1


async def test_outbox_return_exhaustion_is_durable(owners, channel):
    database, tables = owners[0]
    message = command_message()
    async with database.session() as session, session.begin():
        await put_message(session, tables.outbox, message, NOW)
    queue = await channel.get_queue(message.type)
    await queue.unbind(message.type, routing_key=message.type)
    when = NOW
    try:
        for wait in (1, 5, 15, 60, 60):
            assert await publish_batch(database, tables, channel, FixedClock(when)) == 1
            when += timedelta(seconds=wait)
        async with database.session() as session:
            assert await session.scalar(select(tables.outbox.c.state)) == "BLOCKED"
            assert await session.scalar(select(tables.outbox.c.attempts)) == 5
        assert await publish_batch(database, tables, channel, FixedClock(when)) == 0
    finally:
        await queue.bind(message.type, routing_key=message.type)


@pytest.mark.parametrize("table_name", ["outbox", "inbox"])
@pytest.mark.parametrize(
    "values",
    [{"attempts": 6}, {"state": "UNKNOWN"}, {"body": b"x" * 65537}, {"body_sha256": "bad"}],
)
async def test_database_enforces_transport_constraints(owners, table_name, values):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    database, tables = owners[0]
    table = getattr(tables, table_name)
    async with database.session() as session, session.begin():
        await put_message(session, table, command_message(), NOW)
    with pytest.raises(IntegrityError):
        async with database.session() as session, session.begin():
            await session.execute(update(table).values(**values))


async def test_conflict_quarantine_preserves_original(owners, channel):
    database, tables = owners[0]
    message = command_message()
    async with database.session() as session, session.begin():
        await put_message(session, tables.inbox, message, NOW)
    await publish(channel, message.model_copy(update={"request_id": UUID(int=99)}))
    queue = await channel.get_queue(message.type)
    incoming = await queue.get(timeout=5)
    await receive(database, tables, incoming, message.type, NOW)
    async with database.session() as session:
        assert await session.scalar(select(tables.inbox.c.state)) == "PENDING"
        assert (
            await session.scalar(select(tables.quarantine.c.reason)) == "MESSAGE_IDENTITY_CONFLICT"
        )
        assert await session.scalar(select(func.count()).select_from(tables.inbox)) == 1
