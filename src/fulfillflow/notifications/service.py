"""Notification query services and Tracking transaction participant."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.notifications.domain import (
    Notification,
    NotificationRenderer,
    build_notification,
    render_notification,
)
from fulfillflow.notifications.repository import NotificationRepository
from fulfillflow.notifications.schemas import NotificationFilters
from fulfillflow.shared import Clock, Page, new_uuid


class NotificationNotFoundError(LookupError):
    """Raised when an operational Notification identifier does not exist."""

    def __init__(self, notification_id: UUID) -> None:
        self.notification_id = notification_id
        super().__init__(f"Notification {notification_id} was not found.")


class NotificationService:
    """Own transaction boundaries for API-facing Notification queries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = NotificationRepository(session)

    async def get(self, notification_id: UUID) -> Notification:
        """Read one Notification in an explicit transaction."""
        async with self._session.begin():
            notification = await self._repository.get(notification_id)
        if notification is None:
            raise NotificationNotFoundError(notification_id)
        return notification

    async def list(
        self,
        filters: NotificationFilters,
        *,
        page: int,
        page_size: int,
    ) -> Page[Notification]:
        """Read a stable filtered Notification page."""
        async with self._session.begin():
            return await self._repository.list(
                status=filters.status,
                shipment_id=filters.shipment_id,
                created_from=filters.created_from,
                created_to=filters.created_to,
                page=page,
                page_size=page_size,
            )


class NotificationsPublic:
    """Transaction participant used by Tracking without owning its boundary."""

    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        renderer: NotificationRenderer = render_notification,
    ) -> None:
        self._clock = clock
        self._renderer = renderer
        self._repository = NotificationRepository(session)

    async def record_applied_transition(
        self,
        *,
        shipment_id: UUID,
        tracking_event_id: UUID,
        recipient: str,
        resulting_status: str,
    ) -> Notification:
        """Flush one simulation record inside the Core coordinator's local transaction."""
        notification = build_notification(
            notification_id=new_uuid(),
            shipment_id=shipment_id,
            tracking_event_id=tracking_event_id,
            recipient=recipient,
            resulting_status=resulting_status,
            created_at=self._clock.now(),
            renderer=self._renderer,
        )
        await self._repository.add(notification)
        return notification
