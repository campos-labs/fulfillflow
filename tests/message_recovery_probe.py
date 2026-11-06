"""Fresh-process recovery probe for already acknowledged technical inbox work."""

import os

from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import DatabaseSettings
from fulfillflow.contracts.messages import MessageEnvelope
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.db import Database
from fulfillflow.messaging.store import process_one, put_message
from fulfillflow.tracking.message_tables import tables as tracking_tables
from tests.message_support import NOW, result_message


async def recover() -> None:
    database = Database.from_settings(DatabaseSettings(_env_file=None))
    tables = core_tables if os.environ["PROBE_OWNER"] == "core" else tracking_tables

    async def apply(session: AsyncSession, envelope: MessageEnvelope) -> None:
        # A durable SQL effect is committed with DONE, without another broker delivery.
        await put_message(session, tables.outbox, result_message(90), NOW)

    try:
        async with database.session() as session, session.begin():
            assert await process_one(session, tables.inbox, NOW, apply)
    finally:
        await database.dispose()


if __name__ == "__main__":
    run_async(recover())
