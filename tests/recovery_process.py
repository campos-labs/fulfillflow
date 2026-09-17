"""Test-only fatal interruptions inside real worker processes; no runtime fault switches."""

import argparse
import asyncio
import os
import signal
from pathlib import Path

from aio_pika.message import IncomingMessage
from sqlalchemy import event
from sqlalchemy.orm import Session

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import Settings
from fulfillflow.core.message_handler import apply_command
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging import amqp, worker
from fulfillflow.notifications.message_handler import apply_notification
from fulfillflow.notifications.message_tables import tables as notification_tables
from tests.message_support import NOW
from tests.support import FixedClock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cut", required=True)
    parser.add_argument("--marker", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    worker.SystemClock = lambda: FixedClock(NOW)

    def interrupt():
        args.marker.write_text(args.cut)
        os._exit(73)  # Abrupt process loss: bypass Python cleanup and SQL/AMQP finalizers.

    def transaction_cut(session):
        if session.in_nested_transaction():
            return
        scope = session.info.get("cut_scope")
        if scope and args.cut.endswith(scope + "_commit"):
            interrupt()

    if args.cut.startswith("before_"):
        event.listen(Session, "before_commit", transaction_cut)
    elif args.cut.startswith("after_"):
        event.listen(Session, "after_commit", transaction_cut)

    original_put = amqp.put_message

    async def put(session, *values):
        await original_put(session, *values)
        session.info["cut_scope"] = "inbox"

    amqp.put_message = put
    original_publish = amqp.publish

    async def publish(channel, message):
        if message.type == "shipment.status_changed.v1" and args.cut == "before_publish":
            interrupt()
        await original_publish(channel, message)
        if message.type == "shipment.status_changed.v1" and args.cut == "after_confirm":
            interrupt()

    amqp.publish = publish
    original_ack = IncomingMessage.ack

    async def ack(self, *values, **kwargs):
        await original_ack(self, *values, **kwargs)
        if args.cut == "after_ack":
            # A channel RPC after ACK establishes that the broker has observed it.
            await self.channel.queue_declare(queue="shipment.status_changed.v1.queue", passive=True)
            interrupt()

    IncomingMessage.ack = ack
    if args.cut in ("before_inbox_commit", "after_inbox_commit", "after_ack", "shutdown_busy"):

        async def hold(*values, **kwargs):
            if args.cut == "shutdown_busy":
                args.marker.write_text(args.cut)
            await asyncio.sleep(60)

        worker.process_one = hold
    elif args.cut == "fatal_loop":

        async def fail(*values, **kwargs):
            args.marker.write_text(args.cut)
            raise RuntimeError("synthetic mandatory loop failure")

        worker.process_one = fail

    async def application(session, message, clock):
        target = apply_command if settings.service_role == "core" else apply_notification
        await target(session, message, clock)
        session.info["cut_scope"] = "business"

    tables = core_tables if settings.service_role == "core" else notification_tables

    async def run():
        async def stop_watcher():
            for _ in range(1200):
                if args.marker.with_suffix(".stop").exists():
                    signal.raise_signal(signal.SIGTERM)
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(stop_watcher())
        try:
            await worker.serve(settings, tables, application)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    run_async(run())


if __name__ == "__main__":
    main()
