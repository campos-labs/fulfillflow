"""Offline import of original terminals into the new owner's database."""

import argparse
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import DatabaseSettings
from fulfillflow.contracts.notifications import LegacyNotificationArchive, NotificationRead
from fulfillflow.db import Database
from fulfillflow.notifications.message_tables import tables
from fulfillflow.notifications.owned_models import OwnedNotificationModel


class LegacyImportError(ValueError):
    """An offline import prerequisite or full-content comparison failed."""


async def import_legacy(
    session: AsyncSession, archive: LegacyNotificationArchive, *, expected_database: str
) -> int:
    """Import a complete offline export atomically; retries preserve original content."""
    async with session.begin():
        database_name, revision = (
            await session.execute(
                text("SELECT current_database(), (SELECT version_num FROM alembic_version)")
            )
        ).one()
        if database_name != expected_database or revision != "1301_notifications":
            raise LegacyImportError("WRONG_NOTIFICATIONS_DATABASE_OR_HEAD")
        if await session.scalar(select(func.count()).select_from(tables.inbox)):
            raise LegacyImportError("NOTIFICATIONS_ALREADY_RECEIVING")
        for record in archive.notifications:
            await session.execute(
                insert(OwnedNotificationModel)
                .values(**record.model_dump(), origin="LEGACY", message_id=None)
                .on_conflict_do_nothing()
            )
            original = await session.get(OwnedNotificationModel, record.id)
            if (
                original is None
                or original.origin != "LEGACY"
                or original.message_id is not None
                or NotificationRead.model_validate(original) != record
            ):
                raise LegacyImportError("LEGACY_CONTENT_CONFLICT")
        count = await session.scalar(select(func.count()).select_from(OwnedNotificationModel))
        if count != len(archive.notifications):
            raise LegacyImportError("LEGACY_COUNT_CONFLICT")
    return len(archive.notifications)


async def _import(archive: LegacyNotificationArchive, expected_database: str) -> None:
    database = Database.from_settings(DatabaseSettings())
    try:
        async with database.session() as session:
            await import_legacy(session, archive, expected_database=expected_database)
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected-database", required=True)
    args = parser.parse_args()
    try:
        archive = LegacyNotificationArchive.model_validate_json(args.input.read_bytes())
        run_async(_import(archive, args.expected_database))
    except Exception:
        raise SystemExit("Legacy import failed; verify offline cutover prerequisites.") from None


if __name__ == "__main__":
    main()
