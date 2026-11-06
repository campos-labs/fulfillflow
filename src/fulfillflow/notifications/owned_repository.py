"""Persistence and queries scoped exclusively to the Notifications database."""

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.notifications import (
    NotificationList,
    NotificationOrigin,
    NotificationRead,
    NotificationStatus,
)
from fulfillflow.notifications.domain import Notification
from fulfillflow.notifications.owned_models import OwnedNotificationModel


@dataclass(frozen=True)
class StoredNotification:
    record: NotificationRead
    origin: NotificationOrigin
    message_id: UUID | None


def stored(model: OwnedNotificationModel) -> StoredNotification:
    return StoredNotification(
        NotificationRead.model_validate(model),
        cast(NotificationOrigin, model.origin),
        model.message_id,
    )


class OwnedNotificationRepository:
    """Never commits or rolls back the owning operation's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, notification_id: UUID) -> StoredNotification | None:
        model = await self._session.get(OwnedNotificationModel, notification_id)
        return stored(model) if model is not None else None

    async def for_event(self, event_id: UUID) -> StoredNotification | None:
        model = await self._session.scalar(
            select(OwnedNotificationModel).where(
                OwnedNotificationModel.tracking_event_id == event_id
            )
        )
        return stored(model) if model is not None else None

    async def add(self, notification: Notification, message_id: UUID) -> None:
        record = NotificationRead.model_validate(notification)
        self._session.add(
            OwnedNotificationModel(**record.model_dump(), origin="ASYNC", message_id=message_id)
        )
        await self._session.flush()

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
        predicates = []
        if status is not None:
            predicates.append(OwnedNotificationModel.status == status)
        if shipment_id is not None:
            predicates.append(OwnedNotificationModel.shipment_id == shipment_id)
        if created_from is not None:
            predicates.append(OwnedNotificationModel.created_at >= created_from)
        if created_to is not None:
            predicates.append(OwnedNotificationModel.created_at <= created_to)
        total = await self._session.scalar(
            select(func.count()).select_from(OwnedNotificationModel).where(*predicates)
        )
        models = await self._session.scalars(
            select(OwnedNotificationModel)
            .where(*predicates)
            .order_by(OwnedNotificationModel.created_at.desc(), OwnedNotificationModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return NotificationList(
            items=[NotificationRead.model_validate(model) for model in models],
            page=page,
            page_size=page_size,
            total=total or 0,
        )
