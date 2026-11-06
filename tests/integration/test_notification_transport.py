"""Real transport and service-owned SQL boundaries for the Notifications event."""

import os
from datetime import timedelta
from uuid import UUID

import aio_pika
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSessionTransaction
from tests.message_support import NOW, result_message
from tests.notification_support import notification_message
from tests.support import FixedClock

from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging.amqp import declare_flow, publish, publish_batch, receive
from fulfillflow.messaging.store import claim_publications, process_one, put_message
from fulfillflow.notifications.message_tables import tables as notification_tables
from fulfillflow.tracking.message_tables import tables as tracking_tables

FLOW = "shipment.status_changed.v1"


@pytest.fixture
async def notification_channel():
    url = os.environ.get("TEST_AMQP_URL")
    assert url, "TEST_AMQP_URL must identify the isolated real RabbitMQ test vhost"
    connection = await aio_pika.connect(url, timeout=10)
    async with connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        for flow in (FLOW, "tracking.result.v1"):
            await declare_flow(channel, flow)
            queue = await channel.get_queue(f"{flow}.queue")
            await queue.purge()
        yield channel


async def test_claim_filters_flow_before_batch_limit(postgres_database):
    async with postgres_database.session() as session, session.begin():
        for number in range(1, 22):
            await put_message(session, core_tables.outbox, notification_message(number), NOW)
        await put_message(session, core_tables.outbox, result_message(), NOW + timedelta(seconds=1))
    async with postgres_database.session() as session, session.begin():
        claimed = await claim_publications(
            session, core_tables.outbox, NOW + timedelta(seconds=1), flow="tracking.result.v1"
        )
        assert [item.message_id for item in claimed] == [result_message().message_id]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(core_tables.outbox)
                .where(core_tables.outbox.c.type == FLOW, core_tables.outbox.c.state == "PENDING")
            )
            == 21
        )
        notifications = await claim_publications(
            session, core_tables.outbox, NOW + timedelta(seconds=1), flow=FLOW, limit=2
        )
        assert [item.message_id for item in notifications] == [
            notification_message(1).message_id,
            notification_message(2).message_id,
        ]


async def test_unroutable_notification_does_not_claim_tracking_result(
    postgres_database, notification_channel
):
    async with postgres_database.session() as session, session.begin():
        await put_message(session, core_tables.outbox, notification_message(), NOW)
        await put_message(session, core_tables.outbox, result_message(), NOW)
    queue = await notification_channel.get_queue(f"{FLOW}.queue")
    await queue.unbind(FLOW, routing_key=FLOW)
    try:
        assert (
            await publish_batch(
                postgres_database, core_tables, notification_channel, FixedClock(NOW), flow=FLOW
            )
            == 1
        )
        async with postgres_database.session() as session:
            rows = (await session.execute(select(core_tables.outbox))).mappings().all()
            by_type = {row["type"]: row for row in rows}
            assert by_type[FLOW]["state"] == "PENDING"
            assert by_type[FLOW]["attempts"] == 1
            assert by_type["tracking.result.v1"]["state"] == "PENDING"
            assert by_type["tracking.result.v1"]["attempts"] == 0
        assert (
            await publish_batch(
                postgres_database,
                core_tables,
                notification_channel,
                FixedClock(NOW),
                flow="tracking.result.v1",
            )
            == 1
        )
        result_queue = await notification_channel.get_queue("tracking.result.v1.queue")
        incoming = await result_queue.get(timeout=5)
        assert incoming.message_id == str(result_message().message_id)
        await incoming.ack()
    finally:
        await queue.bind(FLOW, routing_key=FLOW)


async def test_notification_ack_follows_commit_without_retaining_sql(
    postgres_database, postgres_notifications_database, notification_channel, monkeypatch
):
    database = postgres_notifications_database
    message = notification_message()
    async with postgres_database.session() as session, session.begin():
        await put_message(session, core_tables.outbox, message, NOW)
    assert (
        await publish_batch(
            postgres_database, core_tables, notification_channel, FixedClock(NOW), flow=FLOW
        )
        == 1
    )
    original_ack = aio_pika.IncomingMessage.ack

    async def verify_committed(self, multiple=False):
        assert database.engine.pool.checkedout() == 0
        async with database.session() as session:
            assert await session.scalar(select(notification_tables.inbox.c.state)) == "PENDING"
        await original_ack(self, multiple=multiple)

    monkeypatch.setattr(aio_pika.IncomingMessage, "ack", verify_committed)
    queue = await notification_channel.get_queue(f"{FLOW}.queue")
    await receive(database, notification_tables, await queue.get(timeout=5), FLOW, NOW)
    await publish(notification_channel, message)
    await receive(database, notification_tables, await queue.get(timeout=5), FLOW, NOW)
    assert await queue.get(fail=False) is None
    await database.dispose()

    async def apply(session, envelope):
        assert envelope == message

    async with database.session() as session, session.begin():
        assert await process_one(session, notification_tables.inbox, NOW, apply)
        assert not await process_one(session, notification_tables.inbox, NOW, apply)
        assert await session.scalar(select(notification_tables.inbox.c.state)) == "DONE"
        assert await session.scalar(select(notification_tables.inbox.c.attempts)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(notification_tables.inbox)) == 1
        )


async def test_notification_conflict_preserves_original_and_quarantines(
    postgres_notifications_database, notification_channel
):
    database = postgres_notifications_database
    message = notification_message()
    queue = await notification_channel.get_queue(f"{FLOW}.queue")
    await publish(notification_channel, message)
    await receive(database, notification_tables, await queue.get(timeout=5), FLOW, NOW)
    async with database.session() as session:
        original = await session.scalar(select(notification_tables.inbox.c.body))
    await publish(notification_channel, message.model_copy(update={"request_id": UUID(int=99)}))
    await receive(database, notification_tables, await queue.get(timeout=5), FLOW, NOW)
    async with database.session() as session:
        assert await session.scalar(select(notification_tables.inbox.c.body)) == original
        assert (
            await session.scalar(select(notification_tables.quarantine.c.reason))
            == "MESSAGE_IDENTITY_CONFLICT"
        )
        assert (
            await session.scalar(select(func.count()).select_from(notification_tables.inbox)) == 1
        )


async def test_notification_commit_failure_never_acks(
    postgres_notifications_database, notification_channel, monkeypatch
):
    database = postgres_notifications_database
    await publish(notification_channel, notification_message())
    queue = await notification_channel.get_queue(f"{FLOW}.queue")
    incoming = await queue.get(timeout=5)
    original_exit = AsyncSessionTransaction.__aexit__

    async def uncertain(self, *args):
        await self.rollback()
        raise ConnectionError("injected commit uncertainty")

    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", uncertain)
    with pytest.raises(ConnectionError, match="commit uncertainty"):
        await receive(database, notification_tables, incoming, FLOW, NOW)
    assert not incoming.processed
    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", original_exit)
    async with database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(notification_tables.inbox)) == 0
        )
    await incoming.reject(requeue=True)
    await receive(database, notification_tables, await queue.get(timeout=5), FLOW, NOW)
    async with database.session() as session:
        assert await session.scalar(select(notification_tables.inbox.c.state)) == "PENDING"


@pytest.mark.parametrize("target", ["core_inbox", "tracking_inbox", "tracking_outbox"])
async def test_notification_event_cannot_enter_tracking_tables(
    postgres_database, postgres_tracking_database, target
):
    database, table = {
        "core_inbox": (postgres_database, core_tables.inbox),
        "tracking_inbox": (postgres_tracking_database, tracking_tables.inbox),
        "tracking_outbox": (postgres_tracking_database, tracking_tables.outbox),
    }[target]
    with pytest.raises(IntegrityError):
        async with database.session() as session, session.begin():
            await put_message(session, table, notification_message(), NOW)
