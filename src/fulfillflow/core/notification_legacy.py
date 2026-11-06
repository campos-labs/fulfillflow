"""Offline Core-owned archive export; never connects to Notifications' database."""

import argparse
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import DatabaseSettings
from fulfillflow.contracts.notifications import LegacyNotificationArchive
from fulfillflow.db import Database


class LegacyExportError(ValueError):
    """The caller must resolve the offline cutover precondition before exporting."""


async def export_legacy(
    session: AsyncSession, *, expected_database: str
) -> LegacyNotificationArchive:
    """Observe the retained Core archive and receipt proofs in one SQL snapshot.

    Writers and webhook admission must already be stopped by the operator. Tracking
    drain is checked separately by its owner, never through a cross-database query.
    """
    async with session.begin():
        row = (
            (
                await session.execute(
                    text("""
            SELECT current_database() AS database_name,
                   (SELECT version_num FROM alembic_version) AS revision,
                   (SELECT count(*) FROM message_inbox WHERE state <> 'DONE')
                     + (SELECT count(*) FROM message_outbox WHERE state <> 'SENT') AS pending,
                   COALESCE((SELECT jsonb_agg(to_jsonb(n) ORDER BY n.id)
                             FROM notifications n), '[]'::jsonb) AS notifications,
                   COALESCE((SELECT jsonb_agg(event_id ORDER BY event_id)
                             FROM tracking_event_receipts
                             WHERE result->>'kind' = 'applied'
                               AND result->>'result' = 'APPLIED'), '[]'::jsonb) AS applied_event_ids
        """)
                )
            )
            .mappings()
            .one()
        )
        if row["database_name"] != expected_database:
            raise LegacyExportError("WRONG_CORE_DATABASE")
        if row["revision"] not in ("1202_core", "1301_core"):
            raise LegacyExportError("UNSUPPORTED_CORE_HEAD")
        if row["pending"]:
            raise LegacyExportError("CORE_WORK_NOT_DRAINED")
        try:
            return LegacyNotificationArchive(
                notifications=row["notifications"], applied_event_ids=row["applied_event_ids"]
            )
        except ValidationError:
            raise LegacyExportError("LEGACY_RECEIPT_OR_CONTENT_CONFLICT") from None


async def _export(expected_database: str) -> LegacyNotificationArchive:
    database = Database.from_settings(DatabaseSettings())
    try:
        async with database.session() as session:
            return await export_legacy(session, expected_database=expected_database)
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-database", required=True)
    args = parser.parse_args()
    try:
        archive = run_async(_export(args.expected_database))
        # Exclusive creation prevents overwriting the operator's previous evidence.
        with args.output.open("x", encoding="utf-8") as destination:
            destination.write(archive.model_dump_json(indent=2))
    except Exception:
        # DSNs, recipient data and peer/database exceptions are never diagnostic output.
        raise SystemExit("Legacy export failed; verify offline cutover prerequisites.") from None


if __name__ == "__main__":
    main()
