"""Compose the Core fact and a later Notifications observation without a shared transaction."""

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.notifications import NotificationPublication, NotificationStatusView
from fulfillflow.contracts.problems import RemoteServiceUnavailableError, ServiceProblemError
from fulfillflow.core.message_tables import tables
from fulfillflow.core.notifications_client import NotificationsClient
from fulfillflow.shared import Clock
from fulfillflow.shipments.public import ShipmentReceipts


class NotificationStatusQuery:
    def __init__(
        self, session: AsyncSession, clock: Clock, notifications: NotificationsClient
    ) -> None:
        self._session = session
        self._clock = clock
        self._notifications = notifications

    async def get(self, event_id: UUID) -> NotificationStatusView:
        async with self._session.begin():
            result = await ShipmentReceipts(self._session).get(event_id)
            publication = await self._session.scalar(
                select(tables.outbox.c.state).where(
                    tables.outbox.c.type == "shipment.status_changed.v1",
                    tables.outbox.c.event_id == event_id,
                )
            )
            observed_at = self._clock.now()
        if result is None:
            raise ServiceProblemError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                title="Resource not found",
                detail=f"Tracking effect {event_id} was not found.",
            )
        if result.kind != "applied" or result.result != "APPLIED":
            if publication is not None:
                raise RemoteServiceUnavailableError
            return NotificationStatusView(
                tracking_event_id=event_id, required=False, core_observed_at=observed_at
            )
        remote = await self._notifications.progress(event_id)
        if (publication is None) != (remote.origin == "LEGACY"):
            raise RemoteServiceUnavailableError
        try:
            return NotificationStatusView(
                tracking_event_id=event_id,
                required=True,
                publication=cast(NotificationPublication | None, publication),
                processing=remote.processing,
                notification_id=remote.notification_id,
                status=remote.status,
                simulated_at=remote.simulated_at,
                origin="LEGACY" if remote.origin == "LEGACY" else "ASYNC",
                core_observed_at=observed_at,
                notifications_observed_at=remote.observed_at,
            )
        except ValueError as error:
            raise RemoteServiceUnavailableError from error
