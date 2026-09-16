"""Drive the actual AMQP/local-SQL stages deterministically in functional tests."""

import os

import aio_pika

from fulfillflow.core.message_handler import apply_command
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.db import Database
from fulfillflow.messaging.amqp import declare_flow, publish_batch, receive
from fulfillflow.messaging.store import process_one
from fulfillflow.shared import Clock
from fulfillflow.tracking.message_handler import apply_result
from fulfillflow.tracking.message_tables import tables as tracking_tables


async def drain(core: Database, tracking: Database, clock: Clock) -> None:
    connection = await aio_pika.connect(os.environ["TEST_AMQP_URL"], timeout=10)
    async with connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        for flow in ("tracking.apply.v1", "tracking.result.v1"):
            await declare_flow(channel, flow)
        for _ in range(20):
            work = 0
            for source, outgoing, target, incoming, flow, handler in (
                (tracking, tracking_tables, core, core_tables, "tracking.apply.v1", apply_command),
                (core, core_tables, tracking, tracking_tables, "tracking.result.v1", apply_result),
            ):
                work += await publish_batch(source, outgoing, channel, clock)
                queue = await channel.get_queue(f"{flow}.queue")
                while message := await queue.get(fail=False, timeout=5):
                    await receive(target, incoming, message, flow, clock.now())
                    work += 1

                async def apply(session, envelope, handler=handler):
                    await handler(session, envelope, clock)

                while True:
                    async with target.session() as session, session.begin():
                        processed = await process_one(session, incoming.inbox, clock.now(), apply)
                    if not processed:
                        break
                    work += 1
            if not work:
                return
        raise AssertionError("Message flow did not become idle within the bounded drain")
