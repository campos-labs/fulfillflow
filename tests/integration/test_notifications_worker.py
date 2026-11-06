"""Real broker/SQL work with independent Core publishers and a consuming-only worker."""

import asyncio
import json
import os

import aio_pika
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from tests.integration.test_notification_permissions import owner_url
from tests.message_support import NOW, result_message
from tests.notification_support import notification_message
from tests.support import FixedClock

from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging import amqp, worker
from fulfillflow.messaging.amqp import declare_flow, publish
from fulfillflow.messaging.health import healthy
from fulfillflow.messaging.store import put_message
from fulfillflow.messaging.worker import serve
from fulfillflow.notifications.message_handler import apply_notification
from fulfillflow.notifications.message_tables import tables as notification_tables
from fulfillflow.notifications.owned_models import OwnedNotificationModel

FLOW = "shipment.status_changed.v1"


async def eventually(check, task):
    async with asyncio.timeout(15):
        while True:
            if task.done():
                await task
                raise AssertionError("Worker stopped before its work completed")
            if await check():
                return
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("failure", ["timeout", "return", "nack"])
async def test_notification_publisher_wait_does_not_hold_tracking_result(
    postgres_database, postgres_settings, monkeypatch, tmp_path, failure
):
    settings = postgres_settings.model_copy(
        update={"amqp_url": SecretStr(os.environ["TEST_AMQP_URL"])}
    )
    path = tmp_path / "core-worker.json"
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(path))
    monkeypatch.setattr(worker, "SystemClock", lambda: FixedClock(NOW))
    notification_waiting = asyncio.Event()
    release_notification = asyncio.Event()
    publisher_channels = {}
    original_publish = amqp.publish

    async def controlled_publish(channel, message):
        publisher_channels[message.type] = channel
        if message.type == FLOW:
            notification_waiting.set()
            await release_notification.wait()
            if failure == "timeout":
                raise TimeoutError("injected notification confirm uncertainty")
            if failure == "nack":
                raise amqp.PublishNotConfirmedError("injected negative confirm")
            await original_publish(channel, message)  # Real mandatory return: queue unbound below.
            raise AssertionError("Unroutable publish unexpectedly succeeded")
        await original_publish(channel, message)

    monkeypatch.setattr(amqp, "publish", controlled_publish)
    async with postgres_database.session() as session, session.begin():
        await put_message(session, core_tables.outbox, notification_message(), NOW)

    async def unused_application(session, message, clock):
        raise AssertionError("No command was admitted by this publisher test")

    stop = asyncio.Event()
    connection = await aio_pika.connect(os.environ["TEST_AMQP_URL"], timeout=5)
    task = asyncio.create_task(serve(settings, core_tables, unused_application, stop=stop))
    try:
        async with connection:
            channel = await connection.channel()
            await declare_flow(channel, "tracking.result.v1")
            if failure == "return":
                queue = await channel.get_queue(f"{FLOW}.queue")
                await queue.unbind(FLOW, routing_key=FLOW)
            await asyncio.wait_for(notification_waiting.wait(), 10)
            # Admit the result only once the Notifications publisher is already waiting.
            async with postgres_database.session() as session, session.begin():
                await put_message(session, core_tables.outbox, result_message(), NOW)
            queue = await channel.get_queue("tracking.result.v1.queue")

            async def result_sent():
                async with postgres_database.session() as session:
                    return (
                        await session.scalar(
                            select(core_tables.outbox.c.state).where(
                                core_tables.outbox.c.type == "tracking.result.v1"
                            )
                        )
                        == "SENT"
                    )

            await eventually(result_sent, task)
            incoming = await queue.get(timeout=5)
            assert incoming.message_id == str(result_message().message_id)
            await incoming.ack()
            assert publisher_channels[FLOW] is not publisher_channels["tracking.result.v1"]
            assert not release_notification.is_set()
            release_notification.set()

            async def notification_paused():
                if failure != "timeout":
                    async with postgres_database.session() as session:
                        return (
                            await session.scalar(
                                select(core_tables.outbox.c.attempts).where(
                                    core_tables.outbox.c.type == FLOW
                                )
                            )
                            == 1
                        )
                if not path.exists():
                    return False
                data = json.loads(path.read_text())
                return data["stages"]["publish_notifications"]["state"] == "dependency_unavailable"

            await eventually(notification_paused, task)
            async with postgres_database.session() as session:
                pending = (
                    (
                        await session.execute(
                            select(core_tables.outbox).where(core_tables.outbox.c.type == FLOW)
                        )
                    )
                    .mappings()
                    .one()
                )
                assert pending["state"] == ("LEASED" if failure == "timeout" else "PENDING")
                assert pending["attempts"] == (0 if failure == "timeout" else 1)
                if failure != "timeout":
                    assert pending["reason"] == "PUBLISH_REJECTED"
    finally:
        release_notification.set()
        stop.set()
        await asyncio.wait_for(task, 16)
        if failure == "return":
            async with await aio_pika.connect(os.environ["TEST_AMQP_URL"], timeout=5) as cleanup:
                await declare_flow(await cleanup.channel(), FLOW)
    assert not healthy(path, "core")


@pytest.mark.parametrize("source", ["broker", "inbox"])
async def test_notifications_worker_consumes_or_resumes_without_publisher(
    postgres_notifications_settings,
    postgres_notifications_database,
    source,
    monkeypatch,
    tmp_path,
):
    database = postgres_notifications_database
    settings = postgres_notifications_settings.model_copy(
        update={"amqp_url": SecretStr(owner_url("notifications"))}
    )
    path = tmp_path / "notifications-worker.json"
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(path))
    monkeypatch.setattr(worker, "SystemClock", lambda: FixedClock(NOW))
    message = notification_message()
    deliveries = []
    original_receive = worker.receive

    async def observed_receive(*args, **kwargs):
        await original_receive(*args, **kwargs)
        deliveries.append(message.message_id)

    monkeypatch.setattr(worker, "receive", observed_receive)
    receiver = await aio_pika.connect(owner_url("notifications"), timeout=5)
    async with receiver:
        channel = await receiver.channel()
        await declare_flow(channel, FLOW)
        queue = await channel.get_queue(f"{FLOW}.queue")
        await queue.purge()

    publisher = await aio_pika.connect(owner_url("core"), timeout=5)
    async with publisher:
        channel = await publisher.channel(publisher_confirms=True, on_return_raises=True)
        await channel.declare_exchange(FLOW, aio_pika.ExchangeType.DIRECT, durable=True)
        if source == "broker":
            await publish(channel, message)
        else:
            async with database.session() as session, session.begin():
                await put_message(session, notification_tables.inbox, message, NOW)
        stop = asyncio.Event()
        task = asyncio.create_task(
            serve(settings, notification_tables, apply_notification, stop=stop)
        )
        try:

            async def simulated():
                async with database.session() as session:
                    return (
                        await session.scalar(select(notification_tables.inbox.c.state)) == "DONE"
                        and await session.scalar(select(OwnedNotificationModel.status))
                        == "SIMULATED"
                        and healthy(path, "notifications")
                    )

            await eventually(simulated, task)
            stages = json.loads(path.read_text())["stages"]
            assert set(stages) == {"receive", "process"}
            async with database.session() as session:
                original = (await session.execute(select(OwnedNotificationModel))).scalar_one()
                original_identity = (original.id, original.created_at, original.simulated_at)
            await publish(channel, message)

            async def duplicate_consumed():
                return len(deliveries) == (2 if source == "broker" else 1)

            await eventually(duplicate_consumed, task)
            async with database.session() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(OwnedNotificationModel))
                    == 1
                )
                assert await session.scalar(select(notification_tables.inbox.c.attempts)) == 1
                preserved = (await session.execute(select(OwnedNotificationModel))).scalar_one()
                assert (
                    preserved.id,
                    preserved.created_at,
                    preserved.simulated_at,
                ) == original_identity
        finally:
            stop.set()
            await asyncio.wait_for(task, 16)
    assert not healthy(path, "notifications")
