"""Local service commands; explicit database guard for mutations, never a public API."""

import argparse
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.db.migrations import schema_is_current
from fulfillflow.messaging.health import healthy, heartbeat_path, observation
from fulfillflow.messaging.operations import RearmError, diagnose, rearm
from fulfillflow.messaging.tables import InboxTables, MessageTables
from fulfillflow.shared import SystemClock

BusinessDiagnostic = Callable[[AsyncSession, datetime, UUID | None], Awaitable[dict[str, Any]]]


RearmGuard = Callable[[AsyncSession, UUID], Awaitable[None]]


def main(
    service: str,
    tables: MessageTables | InboxTables,
    business: BusinessDiagnostic | None = None,
    guard: RearmGuard | None = None,
) -> None:
    parser = argparse.ArgumentParser(description="Owner-local durable transport operations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("healthcheck")
    diagnostic = commands.add_parser("diagnose")
    diagnostic.add_argument("--id", type=UUID)
    diagnostic.add_argument(
        "--flow", choices=("tracking.apply.v1", "tracking.result.v1", "shipment.status_changed.v1")
    )
    recovery = commands.add_parser("rearm")
    recovery.add_argument(
        "--stage",
        choices=("inbox", "outbox") if isinstance(tables, MessageTables) else ("inbox",),
        required=True,
    )
    recovery.add_argument(
        "--flow", choices=("tracking.apply.v1", "tracking.result.v1", "shipment.status_changed.v1")
    )
    recovery.add_argument("--id", type=UUID, required=True)
    recovery.add_argument("--expected-hash", required=True)
    recovery.add_argument("--reason", required=True)
    recovery.add_argument("--expected-database", required=True)
    args = parser.parse_args()
    if args.command == "healthcheck":
        raise SystemExit(0 if healthy(heartbeat_path(service), service) else 1)

    async def execute() -> dict[str, Any]:
        settings = Settings()
        if settings.service_role != service:
            raise RearmError("OWNER_MISMATCH")
        allowed_flows = {
            "core": {"tracking.apply.v1", "tracking.result.v1", "shipment.status_changed.v1"},
            "tracking": {"tracking.apply.v1", "tracking.result.v1"},
            "notifications": {"shipment.status_changed.v1"},
        }
        if args.flow is not None and args.flow not in allowed_flows[service]:
            raise RearmError("FLOW_OWNER_MISMATCH")
        database = Database.from_settings(settings)
        try:
            if not await schema_is_current(database.engine, Path(f"alembic_{service}.ini")):
                raise RearmError("SCHEMA_NOT_CURRENT")
            now = SystemClock().now()
            async with database.session() as session, session.begin():
                database_name = await session.scalar(text("SELECT current_database()"))
                if args.command == "rearm":
                    if database_name != args.expected_database:
                        raise RearmError("DATABASE_MISMATCH")
                    if guard is not None:
                        await guard(session, args.id)
                    generation = await rearm(
                        session,
                        tables,
                        args.stage,
                        args.id,
                        args.expected_hash,
                        args.reason,
                        now,
                        args.flow,
                    )
                    return dict(
                        service=service,
                        database=database_name,
                        stage=args.stage,
                        message_id=args.id,
                        body_sha256=args.expected_hash,
                        reason=args.reason.strip(),
                        generation=generation,
                    )
                report = await diagnose(session, tables, now, args.id, args.flow)
                if business is not None:
                    report["business"] = await business(session, now, args.id)
                return dict(
                    service=service,
                    database=database_name,
                    observed_at=now,
                    flow=args.flow,
                    worker_healthy=healthy(heartbeat_path(service), service),
                    worker=observation(heartbeat_path(service), service),
                    broker="Separate observation: rabbitmqctl list_queues; not a completion signal",
                    counts_are_per_stage=True,
                    **report,
                )
        finally:
            await database.dispose()

    try:
        result = run_async(execute())
    except RearmError as error:
        print(json.dumps({"error": str(error)}))
        raise SystemExit(2) from None
    except Exception:
        print(json.dumps({"error": "OPERATION_UNAVAILABLE"}))
        raise SystemExit(1) from None
    print(json.dumps(result, default=str, sort_keys=True))
