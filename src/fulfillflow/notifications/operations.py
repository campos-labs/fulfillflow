"""Notifications-owned diagnostics and terminal-preserving recovery guards."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.messaging.cli import main
from fulfillflow.messaging.operations import RearmError
from fulfillflow.notifications.message_tables import tables
from fulfillflow.notifications.owned_models import OwnedNotificationModel


async def guard_rearm(session: AsyncSession, message_id: UUID) -> None:
    # Acquire the same lock as the processor before checking terminal business state.
    event_id = await session.scalar(
        select(tables.inbox.c.event_id)
        .where(tables.inbox.c.message_id == message_id)
        .with_for_update()
    )
    if (
        event_id is not None
        and await session.scalar(
            select(OwnedNotificationModel.id).where(
                OwnedNotificationModel.tracking_event_id == event_id
            )
        )
        is not None
    ):
        raise RearmError("TERMINAL_NOTIFICATION")


async def business_diagnostic(
    session: AsyncSession, now: datetime, identity: UUID | None
) -> dict[str, Any]:
    model = OwnedNotificationModel
    counts = [
        dict(row)
        for row in (
            await session.execute(
                select(model.origin, model.status, func.count().label("count")).group_by(
                    model.origin, model.status
                )
            )
        ).mappings()
    ]
    report: dict[str, Any] = {"terminal_counts": counts, "observed_at": now}
    if identity is not None:
        report["terminals"] = [
            dict(row)
            for row in (
                await session.execute(
                    select(
                        model.id,
                        model.tracking_event_id,
                        model.message_id,
                        model.origin,
                        model.status,
                        model.created_at,
                        model.simulated_at,
                    ).where(
                        or_(
                            model.id == identity,
                            model.tracking_event_id == identity,
                            model.message_id == identity,
                        )
                    )
                )
            ).mappings()
        ]
    return report


if __name__ == "__main__":
    main("notifications", tables, business_diagnostic, guard_rearm)
