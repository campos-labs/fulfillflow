"""Pure rendering compatibility for historical datasets; runtime uses contracts."""

from fulfillflow.notifications.domain import (
    ExpectedNotificationFailure,
    Notification,
    NotificationChannel,
    NotificationRenderer,
    NotificationStatus,
    RenderedNotification,
    build_notification,
    render_notification,
)

__all__ = [
    "ExpectedNotificationFailure",
    "Notification",
    "NotificationChannel",
    "NotificationRenderer",
    "NotificationStatus",
    "RenderedNotification",
    "build_notification",
    "render_notification",
]
