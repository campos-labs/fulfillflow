"""Add Orders and Shipments schema with the minimal Carrier registry.

Revision ID: 0002_orders_shipments
Revises: 0001_bootstrap
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_orders_shipments"
down_revision: str | None = "0001_bootstrap"
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


def downgrade() -> None:
    """Safely remove only the tables introduced by this revision."""
    op.drop_index("ix_shipments_status_updated_at", table_name="shipments")
    op.drop_index("ix_shipments_order_id", table_name="shipments")
    op.drop_table("shipments")
    op.drop_table("carriers")
    op.drop_index("ix_orders_status_created_at", table_name="orders")
    op.drop_table("orders")
