"""Required loops, bounded dependency pauses and process-owned shutdown."""

import asyncio
import os
import signal
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from functools import partial
from pathlib import Path
from typing import Any

import aio_pika
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.config import Settings
from fulfillflow.contracts.messages import MessageEnvelope
from fulfillflow.db import Database
from fulfillflow.db.migrations import SchemaNotCurrentError, schema_is_current
from fulfillflow.messaging.amqp import DEPENDENCY_ERRORS, declare_flow, publish_batch, receive
from fulfillflow.messaging.health import Heartbeat, heartbeat_path
from fulfillflow.messaging.store import process_one
from fulfillflow.messaging.tables import InboxTables, MessageTables
from fulfillflow.messaging.telemetry import configure, emit
from fulfillflow.shared import Clock, SystemClock

Application = Callable[[AsyncSession, MessageEnvelope, Clock], Awaitable[None]]


async def pause(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


async def supervise(
    loops: dict[str, Callable[[], Coroutine[Any, Any, None]]],
    stop: asyncio.Event,
    heartbeat: Heartbeat,
    *,
    grace: float = 15,
    on_shutdown: Callable[[], None] | None = None,
) -> None:
    tasks = {asyncio.create_task(loop(), name=name) for name, loop in loops.items()}
    stopped = asyncio.create_task(stop.wait())
    failure = False
    try:
        while not stop.is_set():
            heartbeat.write()
            done, _ = await asyncio.wait(
                tasks | {stopped}, timeout=0.5, return_when=asyncio.FIRST_COMPLETED
            )
            if done & tasks and not stop.is_set():
                failure = True  # Even a normal return is unexpected unless shutdown began.
                stop.set()
        if on_shutdown is not None:
            on_shutdown()
        heartbeat.stopping = True
        heartbeat.write()
        _, pending = await asyncio.wait(tasks, timeout=max(0, grace - 1))
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=min(grace, 1))
        if pending:
            raise RuntimeError("WORKER_SHUTDOWN_TIMEOUT")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if failure or any(isinstance(result, Exception) for result in results):
            raise RuntimeError("REQUIRED_LOOP_STOPPED")
    finally:
        stopped.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(stopped, *tasks, return_exceptions=True)
        heartbeat.stopping = True
        heartbeat.write()


async def serve(
    settings: Settings,
    tables: MessageTables | InboxTables,
    application: Application,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    if settings.amqp_url is None:
        raise ValueError("AMQP_URL is required for workers")
    service = settings.service_role
    if tables.owner != service:
        raise ValueError("Worker tables must belong to its configured service")
    outbound: tuple[tuple[str, str], ...]
    if service == "core":
        inbound = "tracking.apply.v1"
        outbound = (
            ("publish", "tracking.result.v1"),
            ("publish_notifications", "shipment.status_changed.v1"),
        )
    elif service == "tracking":
        inbound = "tracking.result.v1"
        outbound = (("publish", "tracking.apply.v1"),)
    else:
        inbound = "shipment.status_changed.v1"
        outbound = ()
    if outbound and not isinstance(tables, MessageTables):
        raise ValueError("Publishing workers require their own outbox")
    configure()
    url = settings.amqp_url.get_secret_value()
    database = Database.from_settings(settings)
    clock = SystemClock()
    shutdown = stop or asyncio.Event()
    heartbeat = Heartbeat(heartbeat_path(service), service)
    watchdog: threading.Timer | None = None

    def start_watchdog() -> None:
        nonlocal watchdog
        if stop is None and watchdog is None:
            # Last resort: cooperative cancellation or driver cleanup must not exceed 15 s.
            watchdog = threading.Timer(15, lambda: os._exit(1))
            watchdog.daemon = True
            watchdog.start()

    previous: dict[int, Any] = {}
    if stop is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(shutdown.set))

    async def dependency(stage: str, delay: int) -> int:
        heartbeat.record(stage, "dependency_unavailable")
        emit(service, stage, "paused", category="DEPENDENCY_UNAVAILABLE")
        await pause(shutdown, delay)
        return min(delay * 2, 30)

    async def publishing(stage: str, flow: str, publisher_tables: MessageTables) -> None:
        delay = 1
        while not shutdown.is_set():
            try:
                connection = await aio_pika.connect(url, timeout=5)
                async with connection:
                    channel = await connection.channel(
                        publisher_confirms=True, on_return_raises=True
                    )
                    await channel.declare_exchange(flow, aio_pika.ExchangeType.DIRECT, durable=True)
                    while not shutdown.is_set():
                        if channel.is_closed or connection.is_closed:
                            raise ConnectionError("BROKER_UNAVAILABLE")
                        await publish_batch(
                            database, publisher_tables, channel, clock, flow=flow, stage=stage
                        )
                        heartbeat.record(stage, "ready")
                        delay = 1
                        await pause(shutdown, 0.5)
            except DEPENDENCY_ERRORS:
                delay = await dependency(stage, delay)

    async def receiving() -> None:
        delay = 1
        while not shutdown.is_set():
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
                        pending = asyncio.create_task(messages.__anext__())
                        try:
                            while not shutdown.is_set():
                                done, _ = await asyncio.wait({pending}, timeout=0.5)
                                if done:
                                    incoming = pending.result()
                                    await receive(database, tables, incoming, inbound, clock.now())
                                    delay = 1
                                    if shutdown.is_set():
                                        break
                                    pending = asyncio.create_task(messages.__anext__())
                                if channel.is_closed or connection.is_closed:
                                    raise ConnectionError("BROKER_UNAVAILABLE")
                                heartbeat.record("receive", "ready")
                        finally:
                            pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
            except (*DEPENDENCY_ERRORS, StopAsyncIteration):
                delay = await dependency("receive", delay)

    async def processing() -> None:
        delay = 1

        async def apply(session: AsyncSession, message: MessageEnvelope) -> None:
            await application(session, message, clock)

        while not shutdown.is_set():
            try:
                started = time.monotonic()
                async with database.session() as session, session.begin():
                    worked = await process_one(
                        session, tables.inbox, clock.now(), apply, clock=clock
                    )
                    activity = session.info.get("message_activity")
                if activity:
                    emit(
                        service,
                        "process",
                        activity["state"],
                        category=activity["reason"],
                        message=activity,
                        duration=time.monotonic() - started,
                    )
                heartbeat.record("process", "ready")
                delay = 1
                if not worked:
                    await pause(shutdown, 0.5)
            except (OperationalError, InterfaceError):
                delay = await dependency("process", delay)

    async def initialize_and_process() -> None:
        delay = 1
        while not shutdown.is_set():
            try:
                if not await schema_is_current(database.engine, Path(f"alembic_{service}.ini")):
                    raise SchemaNotCurrentError("worker database schema is not current")
                break
            except (OperationalError, InterfaceError):
                delay = await dependency("process", delay)
        await processing()

    try:
        emit(service, "worker", "starting")
        loops: dict[str, Callable[[], Coroutine[Any, Any, None]]] = {
            "receive": receiving,
            "process": initialize_and_process,
        }
        if isinstance(tables, MessageTables):
            for stage, flow in outbound:
                loops[stage] = partial(publishing, stage, flow, tables)
        await supervise(
            loops,
            shutdown,
            heartbeat,
            on_shutdown=start_watchdog,
        )
        emit(service, "worker", "stopped")
    except Exception:
        emit(service, "worker", "failed", category="REQUIRED_LOOP_STOPPED")
        raise
    finally:
        for previous_sig, handler in previous.items():
            signal.signal(previous_sig, handler)
        try:
            await database.dispose()
        finally:
            if watchdog is not None:
                watchdog.cancel()
