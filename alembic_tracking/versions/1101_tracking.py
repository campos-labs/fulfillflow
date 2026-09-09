"""Initial independent Tracking schema; accepts only a new database."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "1101_tracking"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "carrier_event_inbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("carrier_id", sa.Uuid(), nullable=False),
        sa.Column("external_event_id", sa.String(length=128), nullable=False),
        sa.Column("payload_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("raw_body", sa.LargeBinary(), nullable=False),
        sa.Column("parsed_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("command", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "status IN ('RECEIVED', 'PROCESSED', 'REJECTED')",
            name="inbox_status",
        ),
        sa.CheckConstraint(
            "btrim(external_event_id) <> ''",
            name="external_event_id_nonempty",
        ),
        sa.CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="payload_sha256_lower_hex",
        ),
        sa.CheckConstraint(
            "octet_length(raw_body) <= 65536",
            name="raw_body_max_64_kib",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_carrier_event_inbox"),
        sa.UniqueConstraint(
            "carrier_id",
            "external_event_id",
            name="uq_carrier_event_inbox_carrier_external_event_id",
        ),
    )
    op.create_index(
        "ix_carrier_event_inbox_status_received_at",
        "carrier_event_inbox",
        ["status", "received_at"],
        unique=False,
    )

    op.create_table(
        "tracking_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("inbox_event_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("carrier_id", sa.Uuid(), nullable=False),
        sa.Column("external_status", sa.String(length=120), nullable=False),
        sa.Column("canonical_status", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("location", sa.String(length=240), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("application_result", sa.String(length=40), nullable=False),
        sa.Column("previous_shipment_status", sa.String(length=32), nullable=True),
        sa.Column("resulting_shipment_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_status IN ('PENDING', 'POSTED', 'IN_TRANSIT', "
            "'OUT_FOR_DELIVERY', 'DELIVERED', 'EXCEPTION', 'RETURNED', 'CANCELLED')",
            name="canonical_status",
        ),
        sa.CheckConstraint(
            "application_result IN ('APPLIED', 'NO_STATE_CHANGE', 'IGNORED_STALE', "
            "'IGNORED_INVALID_TRANSITION')",
            name="application_result",
        ),
        sa.CheckConstraint(
            "previous_shipment_status IS NULL OR previous_shipment_status IN "
            "('PENDING', 'POSTED', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', 'DELIVERED', "
            "'EXCEPTION', 'RETURNED', 'CANCELLED')",
            name="previous_shipment_status",
        ),
        sa.CheckConstraint(
            "resulting_shipment_status IN ('PENDING', 'POSTED', 'IN_TRANSIT', "
            "'OUT_FOR_DELIVERY', 'DELIVERED', 'EXCEPTION', 'RETURNED', 'CANCELLED')",
            name="resulting_shipment_status",
        ),
        sa.CheckConstraint(
            "btrim(external_status) <> ''",
            name="external_status_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["inbox_event_id"],
            ["carrier_event_inbox.id"],
            name="fk_tracking_events_inbox_event_id_carrier_event_inbox",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tracking_events"),
        sa.UniqueConstraint(
            "inbox_event_id",
            name="uq_tracking_events_inbox_event_id",
        ),
    )
    op.create_index(
        "ix_tracking_events_shipment_occurred_at_created_at",
        "tracking_events",
        ["shipment_id", sa.text("occurred_at DESC"), sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("tracking_events")
    op.drop_table("carrier_event_inbox")
