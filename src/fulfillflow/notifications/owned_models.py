"""Service-owned simulated delivery records, including explicitly imported legacy history."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fulfillflow.notifications.base import NotificationsBase


class OwnedNotificationModel(NotificationsBase):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("tracking_event_id", name="uq_notifications_tracking_event_id"),
        UniqueConstraint("message_id", name="uq_notifications_message_id"),
        CheckConstraint("channel IN ('EMAIL')", name="notification_channel"),
        CheckConstraint("status IN ('SIMULATED', 'FAILED')", name="notification_status"),
        CheckConstraint("origin IN ('ASYNC', 'LEGACY')", name="notification_origin"),
        CheckConstraint(
            "(origin = 'ASYNC' AND message_id IS NOT NULL) OR "
            "(origin = 'LEGACY' AND message_id IS NULL)",
            name="notification_source",
        ),
        CheckConstraint("btrim(recipient) <> ''", name="recipient_nonempty"),
        CheckConstraint("btrim(template_key) <> ''", name="template_key_nonempty"),
        CheckConstraint("btrim(message) <> ''", name="message_nonempty"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    shipment_id: Mapped[UUID] = mapped_column(nullable=False)
    tracking_event_id: Mapped[UUID] = mapped_column(nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient: Mapped[str] = mapped_column(String(254), nullable=False)
    template_key: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    simulated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("message_inbox.message_id", ondelete="RESTRICT"), nullable=True
    )


Index(
    "ix_notifications_status_created_at",
    OwnedNotificationModel.status,
    OwnedNotificationModel.created_at.desc(),
)
