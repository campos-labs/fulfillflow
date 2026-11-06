"""Add Orders and Shipments schema with the minimal Carrier registry.

Revision ID: 1101_core
Revises: None
Create Date: 2026-08-29
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1101_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create module-owned tables, restrictive FKs, checks and required indexes."""
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_reference", sa.String(length=64), nullable=False),
        sa.Column("recipient_name", sa.String(length=160), nullable=False),
        sa.Column("recipient_email", sa.String(length=254), nullable=False),
        sa.Column("recipient_postal_code", sa.String(length=16), nullable=False),
        sa.Column("recipient_city", sa.String(length=120), nullable=False),
        sa.Column("recipient_state", sa.CHAR(length=2), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('CREATED', 'CONFIRMED', 'FULFILLED', 'CANCELLED')",
            name="order_status",
        ),
        sa.CheckConstraint(
            "btrim(external_reference) <> ''",
            name="external_reference_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(recipient_name) <> ''",
            name="recipient_name_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(recipient_email) <> ''",
            name="recipient_email_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(recipient_postal_code) <> ''",
            name="recipient_postal_code_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(recipient_city) <> ''",
            name="recipient_city_nonempty",
        ),
        sa.CheckConstraint(
            "recipient_state = upper(recipient_state) "
            "AND char_length(recipient_state) = 2 AND btrim(recipient_state) <> ''",
            name="recipient_state_upper",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.UniqueConstraint(
            "external_reference",
            name="uq_orders_external_reference",
        ),
    )
    op.create_index(
        "ix_orders_status_created_at",
        "orders",
        ["status", sa.text("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "carriers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("adapter_key", sa.String(length=64), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "code ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="carrier_code_slug",
        ),
        sa.CheckConstraint(
            "btrim(name) <> ''",
            name="carrier_name_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(adapter_key) <> ''",
            name="adapter_key_nonempty",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_carriers"),
        sa.UniqueConstraint("adapter_key", name="uq_carriers_adapter_key"),
        sa.UniqueConstraint("code", name="uq_carriers_code"),
    )

    op.create_table(
        "shipments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("carrier_id", sa.Uuid(), nullable=False),
        sa.Column("tracking_code", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_event_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_external_event_id", sa.String(length=128), nullable=True),
        sa.Column("estimated_delivery_date", sa.Date(), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'POSTED', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', "
            "'DELIVERED', 'EXCEPTION', 'RETURNED', 'CANCELLED')",
            name="shipment_status",
        ),
        sa.CheckConstraint(
            "tracking_code = upper(btrim(tracking_code)) AND tracking_code <> ''",
            name="tracking_code_normalized",
        ),
        sa.CheckConstraint(
            "(status_event_received_at IS NULL AND status_external_event_id IS NULL) OR "
            "(status_event_received_at IS NOT NULL AND status_external_event_id IS NOT NULL)",
            name="external_ordering_complete",
        ),
        sa.CheckConstraint(
            "status_external_event_id IS NULL OR status_external_event_id <> ''",
            name="external_event_id_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            name="fk_shipments_carrier_id_carriers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_shipments_order_id_orders",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shipments"),
        sa.UniqueConstraint(
            "carrier_id",
            "tracking_code",
            name="uq_shipments_carrier_tracking_code",
        ),
    )
    op.create_index("ix_shipments_order_id", "shipments", ["order_id"], unique=False)
    op.create_index(
        "ix_shipments_status_updated_at",
        "shipments",
        ["status", sa.text("updated_at DESC")],
        unique=False,
    )

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

    op.create_table(
        "tracking_event_receipts",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("carrier_id", sa.Uuid(), nullable=False),
        sa.Column("external_event_id", sa.String(128), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="pk_tracking_event_receipts"),
        sa.UniqueConstraint(
            "carrier_id", "external_event_id", name="uq_tracking_receipts_carrier_event"
        ),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="receipt_hash_lower_hex"),
        sa.CheckConstraint(
            "btrim(external_event_id) <> ''", name="receipt_external_event_nonempty"
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            ondelete="RESTRICT",
            name="fk_tracking_receipts_carrier_id_carriers",
        ),
    )
    carriers = sa.table(
        "carriers",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("adapter_key", sa.String()),
        sa.column("active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime(2026, 8, 28, tzinfo=UTC)
    op.bulk_insert(
        carriers,
        [
            dict(
                id=UUID(identifier),
                code=code,
                name=name,
                adapter_key=key,
                active=True,
                created_at=now,
                updated_at=now,
            )
            for identifier, code, name, key in (
                ("00000000-0000-4000-8000-000000000100", "carrier-alpha", "Carrier Alpha", "alpha"),
                ("00000000-0000-4000-8000-000000000101", "carrier-beta", "Carrier Beta", "beta"),
            )
        ],
    )


def downgrade() -> None:
    """Safely remove only the tables introduced by this revision."""
    op.drop_table("tracking_event_receipts")
    op.drop_table("notifications")
    op.drop_index("ix_shipments_status_updated_at", table_name="shipments")
    op.drop_index("ix_shipments_order_id", table_name="shipments")
    op.drop_table("shipments")
    op.drop_table("carriers")
    op.drop_index("ix_orders_status_created_at", table_name="orders")
    op.drop_table("orders")
