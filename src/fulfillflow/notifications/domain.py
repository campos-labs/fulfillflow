"""Framework-independent Notification records and deterministic rendering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class NotificationChannel(StrEnum):
    """Delivery channels represented by the v1.0.0 simulation."""

    EMAIL = "EMAIL"


class NotificationStatus(StrEnum):
    """Outcome of the local notification simulation."""

    SIMULATED = "SIMULATED"
    FAILED = "FAILED"


class ExpectedNotificationFailure(RuntimeError):
    """Known rendering or simulation failure safe to represent as FAILED."""


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    """Deterministic template selection and rendered content."""

    template_key: str
    message: str


type NotificationRenderer = Callable[[str], RenderedNotification]


@dataclass(frozen=True, slots=True)
class Notification:
    """Persistent record of one attempted simulated notification."""

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


_STATUS_CONTENT: dict[str, RenderedNotification] = {
    "PENDING": RenderedNotification(
        template_key="shipment_pending",
        message="Your shipment is pending.",
    ),
    "POSTED": RenderedNotification(
        template_key="shipment_posted",
        message="Your shipment has been posted.",
    ),
    "IN_TRANSIT": RenderedNotification(
        template_key="shipment_in_transit",
        message="Your shipment is in transit.",
    ),
    "OUT_FOR_DELIVERY": RenderedNotification(
        template_key="shipment_out_for_delivery",
        message="Your shipment is out for delivery.",
    ),
    "DELIVERED": RenderedNotification(
        template_key="shipment_delivered",
        message="Your shipment was delivered.",
    ),
    "EXCEPTION": RenderedNotification(
        template_key="shipment_exception",
        message="Your shipment has a delivery exception.",
    ),
    "RETURNED": RenderedNotification(
        template_key="shipment_returned",
        message="Your shipment was returned.",
    ),
    "CANCELLED": RenderedNotification(
        template_key="shipment_cancelled",
        message="Your shipment was cancelled.",
    ),
}

_FAILED_CONTENT = RenderedNotification(
    template_key="shipment_notification_failed",
    message="The shipment notification could not be simulated.",
)
_FAILED_DETAIL = "Notification rendering or simulation failed."


def render_notification(resulting_status: str) -> RenderedNotification:
    """Render only canonical Shipment status content, never carrier payload text."""
    try:
        return _STATUS_CONTENT[resulting_status]
    except KeyError:
        raise ExpectedNotificationFailure from None


def build_notification(
    *,
    notification_id: UUID,
    shipment_id: UUID,
    tracking_event_id: UUID,
    recipient: str,
    resulting_status: str,
    created_at: datetime,
    renderer: NotificationRenderer = render_notification,
) -> Notification:
    """Build a SIMULATED record or a sanitized FAILED record for a known failure."""
    try:
        rendered = renderer(resulting_status)
    except ExpectedNotificationFailure:
        return Notification(
            id=notification_id,
            shipment_id=shipment_id,
            tracking_event_id=tracking_event_id,
            channel=NotificationChannel.EMAIL,
            recipient=recipient.strip(),
            template_key=_FAILED_CONTENT.template_key,
            message=_FAILED_CONTENT.message,
            status=NotificationStatus.FAILED,
            error_detail=_FAILED_DETAIL,
            created_at=created_at,
            simulated_at=None,
        )

    return Notification(
        id=notification_id,
        shipment_id=shipment_id,
        tracking_event_id=tracking_event_id,
        channel=NotificationChannel.EMAIL,
        recipient=recipient.strip(),
        template_key=rendered.template_key,
        message=rendered.message,
        status=NotificationStatus.SIMULATED,
        error_detail=None,
        created_at=created_at,
        simulated_at=created_at,
    )
