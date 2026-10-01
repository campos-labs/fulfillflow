"""One instrumented worker: real handler, transaction barrier, no substitute recovery."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from snapshot import local_snapshot, normalized, require
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import Settings
from fulfillflow.contracts.messages import MessageEnvelope
from fulfillflow.messaging.worker import Application, serve
from fulfillflow.shared import Clock
from support import InstrumentationFailure, controlled_error, verify_application_source

Barrier = Callable[[AsyncSession, MessageEnvelope], Awaitable[None]]
ErrorReporter = Callable[[BaseException, str], Awaitable[None]]


def gated_handler(
    handler: Application,
    barrier: Barrier,
    event_id: str,
    report: ErrorReporter,
) -> Application:
    """Keep product errors distinct from observer failures that must abort SQL."""
    entered = False

    async def preserve_report(exc: BaseException, origin: str) -> None:
        try:
            await report(exc, origin)
        except Exception:
            # Diagnostic export must not turn a product exception into a tooling
            # failure (or replace an instrumentation abort with a catchable error).
            pass

    async def apply(session: AsyncSession, message: MessageEnvelope, clock: Clock) -> None:
        nonlocal entered
        failure: InstrumentationFailure | None = None
        if entered:
            failure = InstrumentationFailure("probe_second_item")
        elif str(message.event_id) != event_id:
            failure = InstrumentationFailure("probe_wrong_target")
        elif not session.in_transaction() or not session.in_nested_transaction():
            failure = InstrumentationFailure("probe_transaction_missing")
        if failure is not None:
            await preserve_report(failure, "instrumentation")
            raise failure
        entered = True
        # Application exceptions retain their identity and the frozen store policy.
        # This block is deliberately outside the instrumentation exception boundary.
        try:
            await handler(session, message, clock)
        except Exception as exc:
            await preserve_report(exc, "application")
            raise
        try:
            await session.flush()
            await barrier(session, message)
        except InstrumentationFailure as instrument_error:
            await preserve_report(instrument_error, "instrumentation")
            raise
        except Exception as exc:
            observer_failure = InstrumentationFailure("probe_instrumentation_failed", exc)
            await preserve_report(observer_failure, "instrumentation")
            raise observer_failure from None

    return apply


async def main() -> None:
    source = os.environ.get("FUNCTIONAL_APPLICATION_SOURCE")
    if not source:
        raise InstrumentationFailure("probe_source_missing")
    owner = os.environ.get("SERVICE_ROLE")
    if owner == "core":
        from fulfillflow.core.message_handler import apply_command as handler
        from fulfillflow.core.message_tables import tables
    elif owner == "tracking":
        from fulfillflow.tracking.message_handler import apply_result as handler
        from fulfillflow.tracking.message_tables import tables
    else:
        raise InstrumentationFailure("probe_wrong_target")
    verify_application_source(Path(source))
    settings = Settings(_env_file=None)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", int(os.environ["BARRIER_PORT"])), timeout=5
    )
    event_id = os.environ["TARGET_EVENT_ID"]
    token = os.environ["BARRIER_TOKEN"]

    async def report(exc: BaseException, origin: str) -> None:
        payload = {
            "marker": "probe_error",
            "pid": os.getpid(),
            "event_id": event_id,
            "owner": owner,
            "origin": origin,
            "error": controlled_error(exc, "probe"),
        }
        # This fallback never exports the ephemeral IPC token or arbitrary error text.
        print(json.dumps(payload), file=sys.stderr, flush=True)
        try:
            writer.write(json.dumps(dict(payload, token=token)).encode() + b"\n")
            await asyncio.wait_for(writer.drain(), timeout=1)
        except (OSError, TimeoutError, RuntimeError):
            pass  # A dead controller must not replace the primary diagnostic/exception.

    async def barrier(session: AsyncSession, message: MessageEnvelope) -> None:
        data = await local_snapshot(session, settings.service_role, event_id)
        inboxes = data["message_inbox"]
        require(len(inboxes) == 1, "WRONG_INBOX_COUNT_AT_BARRIER")
        require(inboxes[0]["message_id"] == str(message.message_id), "WRONG_INBOX_AT_BARRIER")
        require(inboxes[0]["state"] == "PENDING", "DONE_BEFORE_BARRIER")
        if settings.service_role == "core":
            require(len(data["receipts"]) == 1, "NO_RECEIPT_AT_BARRIER")
            require(len(data["notifications"]) == 1, "NO_NOTIFICATION_AT_BARRIER")
            require(len(data["message_outbox"]) == 1, "NO_RESULT_AT_BARRIER")
            effects = await session.execute(
                text(
                    "SELECT s.status AS shipment,o.status AS order_state FROM shipments s "
                    "JOIN orders o ON o.id=s.order_id WHERE s.id=:id"
                ),
                {"id": os.environ["TARGET_SHIPMENT_ID"]},
            )
            require(
                dict(effects.mappings().one())
                == {"shipment": "DELIVERED", "order_state": "FULFILLED"},
                "NO_EFFECTS_AT_BARRIER",
            )
        else:
            require(len(data["timeline"]) == 1, "NO_TIMELINE_AT_BARRIER")
            state = await session.scalar(
                text("SELECT status FROM carrier_event_inbox WHERE id=:id"),
                {"id": str(message.correlation_id)},
            )
            require(state == "PROCESSED", "NO_FINALIZATION_AT_BARRIER")
        transaction = await session.execute(text("SELECT pg_backend_pid(),pg_current_xact_id()"))
        backend, xid = transaction.one()
        marker = normalized(
            {
                "marker": "handler_sql_before_done_and_commit",
                "token": token,
                "pid": os.getpid(),
                "event_id": event_id,
                "owner": settings.service_role,
                "backend_pid": backend,
                "xid": xid,
                "local_snapshot": data,
            }
        )
        writer.write(json.dumps(marker).encode() + b"\n")
        await writer.drain()
        # No SQL or product handler work follows the signal before explicit release.
        # Parent phase/global deadlines are shorter and remain authoritative.
        if await asyncio.wait_for(reader.readline(), timeout=300) != b"release\n":
            raise InstrumentationFailure("probe_barrier_not_released")

    try:
        await serve(settings, tables, gated_handler(handler, barrier, event_id, report))
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1)
        except (OSError, TimeoutError):
            pass


if __name__ == "__main__":
    try:
        run_async(main())
    except BaseException as error:
        print(
            json.dumps({"marker": "probe_exit", "error": controlled_error(error, "probe")}),
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2) from None
