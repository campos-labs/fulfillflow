"""Pydantic schemas exposed by the Notifications HTTP contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from fulfillflow.notifications.domain import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)


@dataclass(frozen=True, slots=True)
class NotificationFilters:
    """Public operational filters supported by the Notification list use case."""

    status: NotificationStatus | None = None
    shipment_id: UUID | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotificationRead(_Schema):
    """Operational Notification projection containing only owned safe fields."""

    id: UUID
    shipment_id: UUID
    tracking_event_id: UUID
    channel: NotificationChannel
    recipient: str
    template_key: str
    message: str
    status: NotificationStatus
    error_detail: str | None
    created_at: datetime
    simulated_at: datetime | None

    @classmethod
    def from_notification(cls, notification: Notification) -> Self:
        """Project a Notification without exposing Tracking or inbox internals."""
        return cls(**asdict(notification))


class NotificationList(_Schema):
    """Stable paginated Notification list response."""

    items: list[NotificationRead]
    page: int
    page_size: int
    total: int
