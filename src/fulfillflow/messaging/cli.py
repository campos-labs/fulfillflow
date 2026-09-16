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
from fulfillflow.messaging.health import healthy, heartbeat_path
from fulfillflow.messaging.operations import RearmError, diagnose, rearm
from fulfillflow.messaging.tables import MessageTables
from fulfillflow.shared import SystemClock

BusinessDiagnostic = Callable[[AsyncSession, datetime, UUID | None], Awaitable[dict[str, Any]]]


def main(service: str, tables: MessageTables, business: BusinessDiagnostic | None = None) -> None:
    parser = argparse.ArgumentParser(description="Owner-local durable transport operations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("healthcheck")
    diagnostic = commands.add_parser("diagnose")
    diagnostic.add_argument("--id", type=UUID)
    recovery = commands.add_parser("rearm")
    recovery.add_argument("--stage", choices=("inbox", "outbox"), required=True)
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
                    generation = await rearm(
                        session, tables, args.stage, args.id, args.expected_hash, args.reason, now
                    )
                    return dict(service=service, message_id=args.id, generation=generation)
                report = await diagnose(session, tables, now, args.id)
                if business is not None:
                    report["business"] = await business(session, now, args.id)
                return dict(
                    service=service,
                    database=database_name,
                    observed_at=now,
                    worker_healthy=healthy(heartbeat_path(service), service),
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
