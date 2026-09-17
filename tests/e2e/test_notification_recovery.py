"""Abrupt process loss at durable boundaries, followed by unmodified worker recovery."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import aio_pika
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging.amqp import declare_flow, publish, publish_batch, receive
from fulfillflow.messaging.health import healthy
from fulfillflow.tracking.message_tables import tables as tracking_tables
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from tests.async_flow import drain
from tests.e2e.test_external_simulator_journey import _stop_application
from tests.notification_support import notification_message
from tests.operations_support import owner_environment
from tests.service_pair import create_app

pytestmark = pytest.mark.integration
FLOW = "shipment.status_changed.v1"


def start(settings, heartbeat, *, cut=None, marker=None):
    environment = owner_environment(settings)
    environment.update(AMQP_URL=os.environ["TEST_AMQP_URL"], WORKER_HEARTBEAT_PATH=str(heartbeat))
    args = [sys.executable, "-m"]
    if cut:
        args += ["tests.recovery_process", "--cut", cut, "--marker", str(marker)]
    else:
        args += [f"fulfillflow.{settings.service_role}.worker"]
    return subprocess.Popen(
        args,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


async def eventually(predicate, *, seconds=20):
    async with asyncio.timeout(seconds):
        for _ in range(1000):
            if await predicate():
                return
            await asyncio.sleep(0.1)
        raise AssertionError("Observation limit exhausted")


async def scalar(database, query):
    async with database.session() as session:
        return await session.scalar(text(query))


async def crashed(process, marker, cut):
    code = await asyncio.to_thread(process.wait, 20)
    assert code == 73 and marker.read_text() == cut


@pytest.mark.parametrize(
    "cut",
    [
        "before_inbox_commit",
        "after_inbox_commit",
        "after_ack",
        "before_business_commit",
        "after_business_commit",
    ],
)
async def test_notifications_process_loss_recovers_exact_terminal_and_empty_queue(
    postgres_database,
    postgres_notifications_database,
    postgres_notifications_settings,
    tmp_path,
    cut,
):
    del postgres_database  # Owns the isolated broker topology/purge fixture.
    database = postgres_notifications_database
    marker, heartbeat = tmp_path / "cut", tmp_path / "heartbeat.json"
    connection = await aio_pika.connect(os.environ["TEST_AMQP_URL"], timeout=5)
    async with connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        await declare_flow(channel, FLOW)
        message = notification_message()
        await publish(channel, message)
        child = start(postgres_notifications_settings, heartbeat, cut=cut, marker=marker)
        try:
            await crashed(child, marker, cut)
        finally:
            await asyncio.to_thread(_stop_application, child)

        async def unhealthy():
            return not healthy(heartbeat, "notifications")

        await eventually(unhealthy, seconds=6)
        count = await scalar(database, "SELECT count(*) FROM message_inbox")
        assert count == (0 if cut == "before_inbox_commit" else 1)
        before = await scalar(database, "SELECT row_to_json(n)::text FROM notifications n")
        assert (before is not None) == (cut == "after_business_commit")
        if cut in ("after_ack", "before_business_commit", "after_business_commit"):
            queue = await channel.declare_queue(f"{FLOW}.queue", passive=True)
            assert queue.declaration_result.message_count == 0
        child = start(postgres_notifications_settings, heartbeat)
        try:

            async def done():
                assert child.poll() is None
                return await scalar(database, "SELECT state FROM message_inbox") == "DONE"

            await eventually(done)
            terminal = await scalar(database, "SELECT row_to_json(n)::text FROM notifications n")
            if before is not None:
                assert terminal == before
            await publish(channel, message)

            async def drained():
                queue = await channel.declare_queue(f"{FLOW}.queue", passive=True)
                return queue.declaration_result.message_count == 0

            await eventually(drained)
            assert (
                await scalar(database, "SELECT row_to_json(n)::text FROM notifications n")
                == terminal
            )
            assert await scalar(database, "SELECT attempts FROM message_inbox") == 1
        finally:
            await asyncio.to_thread(_stop_application, child)


@pytest.mark.parametrize(
    "cut",
    [
        "before_business_commit",
        "after_business_commit",
        "before_publish",
        "after_confirm",
    ],
)
async def test_core_process_loss_preserves_atomic_fact_and_recovers_publication(
    postgres_database,
    postgres_settings,
    postgres_tracking_database,
    postgres_notifications_database,
    postgres_notifications_settings,
    fixed_clock,
    tmp_path,
    cut,
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    children = []
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client, reference="CRASH", carrier_code="carrier-alpha", tracking_code="CRASH"
        )
        response = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="crash-event",
            raw_body=_alpha_body("crash-event", "CRASH", status="DELIVERED"),
        )
        assert response.status_code == 202
        connection = await aio_pika.connect(os.environ["TEST_AMQP_URL"], timeout=5)
        async with connection:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            for flow in ("tracking.apply.v1", "tracking.result.v1", FLOW):
                await declare_flow(channel, flow)
            await publish_batch(postgres_tracking_database, tracking_tables, channel, fixed_clock)
            queue = await channel.get_queue("tracking.apply.v1.queue")
            await receive(
                postgres_database,
                core_tables,
                await queue.get(timeout=5),
                "tracking.apply.v1",
                fixed_clock.now(),
            )
        marker = tmp_path / "cut"
        child = start(postgres_settings, tmp_path / "core.json", cut=cut, marker=marker)
        children.append(child)
        try:
            await crashed(child, marker, cut)
            committed = cut != "before_business_commit"
            assert await scalar(
                postgres_database, "SELECT count(*) FROM tracking_event_receipts"
            ) == int(committed)
            assert await scalar(postgres_database, "SELECT count(*) FROM message_outbox") == (
                2 if committed else 0
            )
            assert await scalar(postgres_database, "SELECT status FROM orders") == (
                "FULFILLED" if committed else "CONFIRMED"
            )
            core = start(postgres_settings, tmp_path / "core-restarted.json")
            notifications = start(postgres_notifications_settings, tmp_path / "notifications.json")
            children.extend([core, notifications])

            async def done():
                assert core.poll() is None and notifications.poll() is None
                return (
                    await scalar(
                        postgres_notifications_database, "SELECT count(*) FROM notifications"
                    )
                    == 1
                )

            await eventually(done)

            # Core publisher restart uses the real clock; the interrupted lease was created
            # with an injected historical Clock, so its expiry is deterministic without SQL edits.
            async def sent():
                return (
                    await scalar(
                        postgres_database, "SELECT count(*) FROM message_outbox WHERE state='SENT'"
                    )
                    == 2
                )

            await eventually(sent)
            for process in reversed(children):
                await asyncio.to_thread(_stop_application, process)
            await drain(postgres_database, postgres_tracking_database, fixed_clock)
            assert (await client.get(response.headers["location"])).json()["status"] == "PROCESSED"
            assert (
                await scalar(postgres_notifications_database, "SELECT count(*) FROM notifications")
                == 1
            )
            assert await scalar(postgres_database, "SELECT count(*) FROM notifications") == 0
        finally:
            for process in reversed(children):
                await asyncio.to_thread(_stop_application, process)


@pytest.mark.parametrize("cut", ["fatal_loop", "shutdown_busy"])
async def test_notifications_real_process_lifecycle(
    postgres_database,
    postgres_notifications_settings,
    tmp_path,
    cut,
):
    del postgres_database
    marker, heartbeat = tmp_path / "marker", tmp_path / "heartbeat.json"
    child = start(postgres_notifications_settings, heartbeat, cut=cut, marker=marker)
    try:

        async def reached():
            return marker.exists()

        await eventually(reached)
        if cut == "fatal_loop":
            assert await asyncio.to_thread(child.wait, 17) != 0
        else:
            # On Windows signal.raise_signal exercises the same registered handler through
            # the process' own test-only watcher; POSIX receives a real SIGTERM directly.
            if sys.platform == "win32":
                marker.with_suffix(".stop").write_text("stop")
            else:
                child.send_signal(signal.SIGTERM)
            started = time.monotonic()
            assert await asyncio.to_thread(child.wait, 17) == 0
            assert time.monotonic() - started < 16
        assert not healthy(heartbeat, "notifications")
        assert json.loads(heartbeat.read_text())["stopping"] is True
    finally:
        await asyncio.to_thread(_stop_application, child)
