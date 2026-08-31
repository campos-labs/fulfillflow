"""Add persistent simulated Notifications.

Revision ID: 0004_notifications
Revises: 0003_carriers_tracking
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_notifications"
down_revision: str | None = "0003_carriers_tracking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the Notifications-owned simulated delivery record."""
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("tracking_event_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("recipient", sa.String(length=254), nullable=False),
        sa.Column("template_key", sa.String(length=80), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("simulated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('EMAIL')",
            name="notification_channel",
        ),
        sa.CheckConstraint(
            "status IN ('SIMULATED', 'FAILED')",
            name="notification_status",
        ),
        sa.CheckConstraint(
            "btrim(recipient) <> ''",
            name="recipient_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(template_key) <> ''",
            name="template_key_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(message) <> ''",
            name="message_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name="fk_notifications_shipment_id_shipments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tracking_event_id"],
            ["tracking_events.id"],
            name="fk_notifications_tracking_event_id_tracking_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.UniqueConstraint(
            "tracking_event_id",
            name="uq_notifications_tracking_event_id",
        ),
    )
    op.create_index(
        "ix_notifications_status_created_at",
        "notifications",
        ["status", sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Safely remove only the Notifications-owned table."""
    op.drop_index(
        "ix_notifications_status_created_at",
        table_name="notifications",
    )
    op.drop_table("notifications")
