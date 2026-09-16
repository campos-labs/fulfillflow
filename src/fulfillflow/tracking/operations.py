"""Tracking-owned operations, including the authoritative accepted-event backlog."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.messaging.cli import main
from fulfillflow.tracking.message_tables import tables
from fulfillflow.tracking.models import CarrierEventInboxModel


async def business_diagnostic(
    session: AsyncSession, now: datetime, identity: UUID | None
) -> dict[str, Any]:
    inbox = CarrierEventInboxModel
    pending = inbox.status == "RECEIVED"
    count, oldest = (
        await session.execute(select(func.count(), func.min(inbox.received_at)).where(pending))
    ).one()
    legacy = await session.scalar(
        select(func.count())
        .select_from(inbox)
        .where(
            pending,
            ~select(tables.outbox.c.message_id)
            .where(tables.outbox.c.correlation_id == inbox.id)
            .exists(),
        )
    )
    items = []
    if identity is not None:
        event_ids = (
            select(tables.inbox.c.correlation_id)
            .where((tables.inbox.c.message_id == identity) | (tables.inbox.c.event_id == identity))
            .union(
                select(tables.outbox.c.correlation_id).where(
                    (tables.outbox.c.message_id == identity)
                    | (tables.outbox.c.event_id == identity)
                )
            )
        )
        items = [
            dict(row)
            for row in (
                await session.execute(
                    select(
                        inbox.id,
                        inbox.request_id,
                        inbox.status,
                        inbox.received_at,
                        inbox.processed_at,
                        inbox.completed_at,
                        inbox.result,
                        inbox.error_code,
                    ).where((inbox.id == identity) | inbox.id.in_(event_ids))
                )
            ).mappings()
        ]
    return dict(
        accepted_nonterminal=count,
        legacy_pending=legacy,
        oldest_age_seconds=(now - oldest).total_seconds() if oldest else None,
        items=items,
    )


if __name__ == "__main__":
    main("tracking", tables, business_diagnostic)
