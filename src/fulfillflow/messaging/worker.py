"""Three required loops; broker recovery never replaces durable local processing."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import aio_pika
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.config import Settings
from fulfillflow.contracts.messages import MessageEnvelope
from fulfillflow.db import Database
from fulfillflow.db.migrations import SchemaNotCurrentError, schema_is_current
from fulfillflow.messaging.amqp import declare_flow, publish_batch, receive
from fulfillflow.messaging.store import process_one
from fulfillflow.messaging.tables import MessageTables
from fulfillflow.shared import Clock, SystemClock

Application = Callable[[AsyncSession, MessageEnvelope, Clock], Awaitable[None]]
_LOG = logging.getLogger(__name__)
_DEPENDENCY_ERRORS = (
    OSError,
    TimeoutError,
    aio_pika.exceptions.AMQPConnectionError,
    aio_pika.exceptions.ChannelInvalidStateError,
    OperationalError,
    InterfaceError,
)


async def serve(settings: Settings, tables: MessageTables, application: Application) -> None:
    if settings.amqp_url is None:
        raise ValueError("AMQP_URL is required for workers")
    url = settings.amqp_url.get_secret_value()
    database = Database.from_settings(settings)
    clock = SystemClock()
    outbound = "tracking.result.v1" if settings.service_role == "core" else "tracking.apply.v1"
    inbound = "tracking.apply.v1" if settings.service_role == "core" else "tracking.result.v1"

    async def publishing() -> None:
        delay = 1
        while True:
            try:
                connection = await aio_pika.connect(url, timeout=5)
                async with connection:
                    channel = await connection.channel(
                        publisher_confirms=True, on_return_raises=True
                    )
                    await channel.declare_exchange(
                        outbound, aio_pika.ExchangeType.DIRECT, durable=True
                    )
                    while True:
                        await publish_batch(database, tables, channel, clock)
                        delay = 1
                        await asyncio.sleep(0.5)
            except _DEPENDENCY_ERRORS:
                _LOG.warning("publication dependency unavailable")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def receiving() -> None:
        delay = 1
        while True:
            try:
                connection = await aio_pika.connect(url, timeout=5)
                async with connection:
                    channel = await connection.channel(
                        publisher_confirms=True, on_return_raises=True
                    )
                    await channel.set_qos(prefetch_count=8)
                    await declare_flow(channel, inbound)
                    queue = await channel.get_queue(f"{inbound}.queue")
                    async with queue.iterator() as messages:
                        async for message in messages:
                            await receive(database, tables, message, inbound, clock.now())
                            delay = 1
            except _DEPENDENCY_ERRORS:
                # Leaving the connection scope closes the channel without ACK on uncertain commit.
                _LOG.warning("reception dependency unavailable")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def processing() -> None:
        delay = 1

        async def apply(session: AsyncSession, message: MessageEnvelope) -> None:
            await application(session, message, clock)

        while True:
            try:
                async with database.session() as session, session.begin():
                    worked = await process_one(session, tables.inbox, clock.now(), apply)
                delay = 1
                if not worked:
                    await asyncio.sleep(0.5)
            except (OperationalError, InterfaceError):
                _LOG.warning("local processing database unavailable")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    try:
        if not await schema_is_current(
            database.engine, Path(f"alembic_{settings.service_role}.ini")
        ):
            raise SchemaNotCurrentError("worker database schema is not current")
        async with asyncio.TaskGroup() as group:
            group.create_task(publishing())
            group.create_task(receiving())
            group.create_task(processing())
    finally:
        await database.dispose()
