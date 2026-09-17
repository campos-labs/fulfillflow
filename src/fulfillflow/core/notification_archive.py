"""Retained Core archive metadata; no active Notification writer or runtime query."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fulfillflow.db.base import Base


class CoreLegacyNotificationModel(Base):
    """Original table retained for verified offline cutover and schema reconstruction."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint(
            "tracking_event_id",
            name="uq_notifications_tracking_event_id",
        ),
        CheckConstraint(
            "channel IN ('EMAIL')",
            name="notification_channel",
        ),
        CheckConstraint(
            "status IN ('SIMULATED', 'FAILED')",
            name="notification_status",
        ),
        CheckConstraint(
            "btrim(recipient) <> ''",
            name="recipient_nonempty",
        ),
        CheckConstraint(
            "btrim(template_key) <> ''",
            name="template_key_nonempty",
        ),
        CheckConstraint(
            "btrim(message) <> ''",
            name="message_nonempty",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "shipments.id",
            ondelete="RESTRICT",
            name="fk_notifications_shipment_id_shipments",
        ),
        nullable=False,
    )
    tracking_event_id: Mapped[UUID] = mapped_column(
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient: Mapped[str] = mapped_column(String(254), nullable=False)
    template_key: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    simulated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


Index(
    "ix_notifications_status_created_at",
    CoreLegacyNotificationModel.status,
    CoreLegacyNotificationModel.created_at.desc(),
)
