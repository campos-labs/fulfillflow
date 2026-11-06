"""Real isolated Docker dependencies return without exhausting item attempts."""

import asyncio
import os
import subprocess

import aio_pika
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from tests.message_support import NOW, command_message, result_message

from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging.amqp import declare_flow, publish, receive
from fulfillflow.messaging.health import healthy
from fulfillflow.messaging.store import put_message
from fulfillflow.messaging.worker import serve
from fulfillflow.tracking.message_tables import tables as tracking_tables


async def docker(*arguments):
    result = await asyncio.to_thread(
        subprocess.run, ["docker", *arguments], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


async def eventually(check):
    async with asyncio.timeout(45):
        for _ in range(225):
            if await check():
                return
            await asyncio.sleep(0.2)
        raise AssertionError("Recovery deadline expired")


@pytest.mark.parametrize("owner", ["core", "tracking"])
@pytest.mark.parametrize("dependency", ["broker", "database"])
@pytest.mark.parametrize("phase", ["startup", "running"])
async def test_dependency_outage_and_return_keeps_item_recoverable(
    postgres_settings,
    postgres_database,
    postgres_tracking_settings,
    postgres_tracking_database,
    monkeypatch,
    tmp_path,
    owner,
    dependency,
    phase,
):
    database, tables, settings, message = (
        (postgres_database, core_tables, postgres_settings, command_message())
        if owner == "core"
        else (
            postgres_tracking_database,
            tracking_tables,
            postgres_tracking_settings,
            result_message(),
        )
    )
    broker = os.environ["TEST_V12_RABBITMQ_CONTAINER"]
    pg = os.environ["TEST_V11_POSTGRES_CONTAINER"]
    # Docker identities must be explicitly isolated test resources.
    assert "test" in broker and "test" in pg
    url = os.environ["TEST_AMQP_URL"]
    path = tmp_path / "worker.json"
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(path))
    settings = settings.model_copy(update={"amqp_url": SecretStr(url)})
    stop = asyncio.Event()
    calls = []

    async def application(session, envelope, clock):
        calls.append(envelope.message_id)

    async with database.session() as session, session.begin():
        await put_message(session, tables.inbox, message, NOW)

    async def stop_dependency():
        if dependency == "broker":
            await docker("exec", broker, "rabbitmqctl", "stop_app")
        else:
            await database.dispose()
            await docker("stop", "--time", "5", pg)

    async def start_dependency():
        if dependency == "broker":
            await docker("exec", broker, "rabbitmqctl", "start_app")
        else:
            await docker("start", pg)

    if phase == "startup":
        await stop_dependency()
    task = asyncio.create_task(serve(settings, tables, application, stop=stop))

    async def is_healthy():
        if task.done():
            await task
        return healthy(path, owner)

    try:
        try:
            if phase == "running":
                await eventually(is_healthy)
                await stop_dependency()

            async def unavailable():
                if task.done():
                    await task
                return path.exists() and not healthy(path, owner)

            await eventually(unavailable)
            assert not task.done()
        finally:
            await start_dependency()
        await eventually(is_healthy)

        async def is_done():
            async with database.session() as session:
                return await session.scalar(select(tables.inbox.c.state)) == "DONE"

        await eventually(is_done)
        async with database.session() as session:
            assert await session.scalar(select(tables.inbox.c.attempts)) == 1
        assert calls == [message.message_id]
        outgoing = result_message(3) if owner == "core" else command_message(3)
        async with database.session() as session, session.begin():
            await put_message(session, tables.outbox, outgoing, NOW)

        async def is_sent():
            async with database.session() as session:
                return await session.scalar(select(tables.outbox.c.state)) == "SENT"

        await eventually(is_sent)
        # Real delivery after recovery is idempotent and does not reapply DONE.
        connection = await aio_pika.connect(url)
        async with connection:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await declare_flow(channel, message.type)
            outgoing_queue = await channel.get_queue(f"{outgoing.type}.queue")
            incoming = await outgoing_queue.get(timeout=5)
            assert incoming.message_id == str(outgoing.message_id)
            await receive(
                postgres_tracking_database if owner == "core" else postgres_database,
                tracking_tables if owner == "core" else core_tables,
                incoming,
                outgoing.type,
                NOW,
            )
            await publish(channel, message)
            await asyncio.sleep(0.7)
        assert calls == [message.message_id]
    finally:
        stop.set()
        await asyncio.wait_for(task, 16)
    assert not healthy(path, owner)
