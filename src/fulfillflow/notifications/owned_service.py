"""Owner-local query boundaries for the independent Notifications API."""

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.notifications import (
    NotificationCounts,
    NotificationList,
    NotificationProcessing,
    NotificationProgress,
    NotificationRead,
    NotificationStatus,
)
from fulfillflow.notifications.message_tables import tables
from fulfillflow.notifications.owned_models import OwnedNotificationModel
from fulfillflow.notifications.owned_repository import OwnedNotificationRepository
from fulfillflow.shared import Clock


class OwnedNotificationNotFoundError(LookupError):
    def __init__(self, notification_id: UUID) -> None:
        super().__init__(f"Notification {notification_id} was not found.")


class NotificationIntegrityError(RuntimeError):
    """A local terminal record and its durable processing state disagree."""


class OwnedNotificationService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._repository = OwnedNotificationRepository(session)

    async def get(self, notification_id: UUID) -> NotificationRead:
        async with self._session.begin():
            existing = await self._repository.get(notification_id)
        if existing is None:
            raise OwnedNotificationNotFoundError(notification_id)
        return existing.record

    async def list(
        self,
        *,
        status: NotificationStatus | None = None,
        shipment_id: UUID | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> NotificationList:
        async with self._session.begin():
            return await self._repository.list(
                status=status,
                shipment_id=shipment_id,
                created_from=created_from,
                created_to=created_to,
                page=page,
                page_size=page_size,
            )

    async def counts(self) -> NotificationCounts:
        async with self._session.begin():
            rows = await self._session.execute(
                select(OwnedNotificationModel.status, func.count()).group_by(
                    OwnedNotificationModel.status
                )
            )
            counts = dict(rows.tuples().all())
            return NotificationCounts(
                simulated=counts.get("SIMULATED", 0),
                failed=counts.get("FAILED", 0),
                observed_at=self._clock.now(),
            )

    async def progress(self, event_id: UUID) -> NotificationProgress:
        async with self._session.begin():
            # One statement observes the terminal and inbox in the same READ COMMITTED snapshot.
            row = (
                await self._session.execute(
                    select(
                        tables.inbox.c.state,
                        OwnedNotificationModel.id,
                        OwnedNotificationModel.status,
                        OwnedNotificationModel.simulated_at,
                        OwnedNotificationModel.origin,
                    )
                    .select_from(
                        tables.inbox.join(
                            OwnedNotificationModel,
                            tables.inbox.c.event_id == OwnedNotificationModel.tracking_event_id,
                            full=True,
                        )
                    )
                    .where(
                        (tables.inbox.c.event_id == event_id)
                        | (OwnedNotificationModel.tracking_event_id == event_id)
                    )
                )
            ).first()
            now = self._clock.now()
        if row is None:
            return NotificationProgress(
                tracking_event_id=event_id,
                processing="NOT_RECEIVED",
                origin=None,
                observed_at=now,
            )
        state, identity, status, simulated_at, origin = row
        try:
            return NotificationProgress(
                tracking_event_id=event_id,
                processing=None if origin == "LEGACY" else cast(NotificationProcessing, state),
                notification_id=identity,
                status=status,
                simulated_at=simulated_at,
                origin="LEGACY" if origin == "LEGACY" else "ASYNC",
                observed_at=now,
            )
        except ValueError:
            raise NotificationIntegrityError("NOTIFICATION_STATE_CONFLICT") from None
