"""Add deterministic Carriers reference data and Tracking persistence.

Revision ID: 0003_carriers_tracking
Revises: 0002_orders_shipments
Create Date: 2026-08-30
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_carriers_tracking"
down_revision: str | None = "0002_orders_shipments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CARRIER_ALPHA_ID = UUID("00000000-0000-4000-8000-000000000100")
_CARRIER_BETA_ID = UUID("00000000-0000-4000-8000-000000000101")
_REFERENCE_DATA_TIMESTAMP = datetime(2026, 8, 28, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _CarrierReference:
    id: UUID
    code: str
    name: str
    adapter_key: str


_CARRIER_REFERENCES = (
    _CarrierReference(
        _CARRIER_ALPHA_ID,
        "carrier-alpha",
        "Carrier Alpha",
        "alpha",
    ),
    _CarrierReference(
        _CARRIER_BETA_ID,
        "carrier-beta",
        "Carrier Beta",
        "beta",
    ),
)


def upgrade() -> None:
    """Install Carrier reference data and create the authenticated event timeline."""
    carriers = sa.table(
        "carriers",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String(length=32)),
        sa.column("name", sa.String(length=120)),
        sa.column("adapter_key", sa.String(length=64)),
        sa.column("active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    existing_rows = (
        bind.execute(
            sa.select(
                carriers.c.id,
                carriers.c.code,
                carriers.c.adapter_key,
            ).where(
                sa.or_(
                    carriers.c.code.in_([item.code for item in _CARRIER_REFERENCES]),
                    carriers.c.id.in_([item.id for item in _CARRIER_REFERENCES]),
                    carriers.c.adapter_key.in_([item.adapter_key for item in _CARRIER_REFERENCES]),
                )
            )
        )
        .mappings()
        .all()
    )
    rows_by_code = {row["code"]: row for row in existing_rows}
    rows_by_id = {row["id"]: row for row in existing_rows}
    rows_by_adapter = {row["adapter_key"]: row for row in existing_rows}

    # 0002 allowed operators to create Carriers and Shipments before these
    # reference rows existed. Validate every official association before any
    # insert so the migration either preserves a compatible registry intact or
    # fails transactionally without a partially installed pair.
    for reference in _CARRIER_REFERENCES:
        existing_by_code = rows_by_code.get(reference.code)
        if (
            existing_by_code is not None
            and existing_by_code["adapter_key"] != reference.adapter_key
        ):
            raise RuntimeError(
                f"Cannot install {reference.code}: its adapter_key must be "
                f"'{reference.adapter_key}'."
            )

        existing_by_id = rows_by_id.get(reference.id)
        if existing_by_id is not None and existing_by_id["code"] != reference.code:
            raise RuntimeError(
                f"Cannot install {reference.code}: deterministic UUID {reference.id} "
                f"is already assigned to code '{existing_by_id['code']}'."
            )

        existing_by_adapter = rows_by_adapter.get(reference.adapter_key)
        if existing_by_adapter is not None and existing_by_adapter["code"] != reference.code:
            raise RuntimeError(
                f"Cannot install {reference.code}: adapter_key "
                f"'{reference.adapter_key}' is already assigned to code "
                f"'{existing_by_adapter['code']}'."
            )

    missing_references = [
        {
            "id": reference.id,
            "code": reference.code,
            "name": reference.name,
            "adapter_key": reference.adapter_key,
            "active": True,
            "created_at": _REFERENCE_DATA_TIMESTAMP,
            "updated_at": _REFERENCE_DATA_TIMESTAMP,
        }
        for reference in _CARRIER_REFERENCES
        if reference.code not in rows_by_code
    ]
    if missing_references:
        bind.execute(sa.insert(carriers), missing_references)

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
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            name="fk_carrier_event_inbox_carrier_id_carriers",
            ondelete="RESTRICT",
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
            ["carrier_id"],
            ["carriers.id"],
            name="fk_tracking_events_carrier_id_carriers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["inbox_event_id"],
            ["carrier_event_inbox.id"],
            name="fk_tracking_events_inbox_event_id_carrier_event_inbox",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name="fk_tracking_events_shipment_id_shipments",
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
    """Remove only Tracking structures while retaining all Carrier data."""
    op.drop_index(
        "ix_tracking_events_shipment_occurred_at_created_at",
        table_name="tracking_events",
    )
    op.drop_table("tracking_events")
    op.drop_index(
        "ix_carrier_event_inbox_status_received_at",
        table_name="carrier_event_inbox",
    )
    op.drop_table("carrier_event_inbox")
    # Reference rows are intentionally retained. 0002 permitted preexisting
    # Carriers and Shipments, so 0003 cannot infer row provenance from a
    # deterministic UUID/code without risking data loss during downgrade.
