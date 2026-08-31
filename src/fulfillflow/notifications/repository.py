"""Notifications-owned persistence operations; this module never commits."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.notifications.domain import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from fulfillflow.notifications.models import NotificationModel
from fulfillflow.shared import Page


class NotificationRepository:
    """Map Notification records to their module-owned SQLAlchemy rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, notification: Notification) -> None:
        """Stage and flush a Notification in the caller's transaction."""
        self._session.add(_to_model(notification))
        await self._session.flush()

    async def get(self, notification_id: UUID) -> Notification | None:
        """Load one operational Notification record."""
        model = await self._session.scalar(
            select(NotificationModel).where(NotificationModel.id == notification_id)
        )
        return _to_entity(model) if model is not None else None

    async def list(
        self,
        *,
        status: NotificationStatus | None,
        shipment_id: UUID | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
    ) -> Page[Notification]:
        """Return a stable newest-first filtered Notification page."""
        predicates = []
        if status is not None:
            predicates.append(NotificationModel.status == status.value)
        if shipment_id is not None:
            predicates.append(NotificationModel.shipment_id == shipment_id)
        if created_from is not None:
            predicates.append(NotificationModel.created_at >= created_from)
        if created_to is not None:
            predicates.append(NotificationModel.created_at <= created_to)

        total = await self._session.scalar(
            select(func.count()).select_from(NotificationModel).where(*predicates)
        )
        statement = (
            select(NotificationModel)
            .where(*predicates)
            .order_by(NotificationModel.created_at.desc(), NotificationModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        models = (await self._session.scalars(statement)).all()
        return Page(
            items=[_to_entity(model) for model in models],
            page=page,
            page_size=page_size,
            total=total or 0,
        )


def _to_model(notification: Notification) -> NotificationModel:
    return NotificationModel(
        id=notification.id,
        shipment_id=notification.shipment_id,
        tracking_event_id=notification.tracking_event_id,
        channel=notification.channel.value,
        recipient=notification.recipient,
        template_key=notification.template_key,
        message=notification.message,
        status=notification.status.value,
        error_detail=notification.error_detail,
        created_at=notification.created_at,
        simulated_at=notification.simulated_at,
    )


def _to_entity(model: NotificationModel) -> Notification:
    return Notification(
        id=model.id,
        shipment_id=model.shipment_id,
        tracking_event_id=model.tracking_event_id,
        channel=NotificationChannel(model.channel),
        recipient=model.recipient,
        template_key=model.template_key,
        message=model.message,
        status=NotificationStatus(model.status),
        error_detail=model.error_detail,
        created_at=model.created_at,
        simulated_at=model.simulated_at,
    )
