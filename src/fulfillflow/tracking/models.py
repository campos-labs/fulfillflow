"""SQLAlchemy models owned exclusively by the Tracking module."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fulfillflow.tracking.base import TrackingBase
from fulfillflow.tracking.domain import JsonValue


class CarrierEventInboxModel(TrackingBase):
    """Forensic record of one authenticated carrier payload."""

    __tablename__ = "carrier_event_inbox"
    __table_args__ = (
        UniqueConstraint(
            "carrier_id",
            "external_event_id",
            name="uq_carrier_event_inbox_carrier_external_event_id",
        ),
        CheckConstraint(
            "status IN ('RECEIVED', 'PROCESSED', 'REJECTED')",
            name="inbox_status",
        ),
        CheckConstraint(
            "btrim(external_event_id) <> ''",
            name="external_event_id_nonempty",
        ),
        CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="payload_sha256_lower_hex",
        ),
        CheckConstraint(
            "octet_length(raw_body) <= 65536",
            name="raw_body_max_64_kib",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    carrier_id: Mapped[UUID] = mapped_column(
        nullable=False,
    )
    external_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    raw_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    parsed_payload: Mapped[JsonValue] = mapped_column(JSONB, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    request_id: Mapped[UUID] = mapped_column(nullable=False)
    command: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB, nullable=True)


Index(
    "ix_carrier_event_inbox_status_received_at",
    CarrierEventInboxModel.status,
    CarrierEventInboxModel.received_at,
)


class TrackingEventModel(TrackingBase):
    """Append-only normalized timeline event for one Shipment."""

    __tablename__ = "tracking_events"
    __table_args__ = (
        UniqueConstraint(
            "inbox_event_id",
            name="uq_tracking_events_inbox_event_id",
        ),
        CheckConstraint(
            "canonical_status IN ('PENDING', 'POSTED', 'IN_TRANSIT', "
            "'OUT_FOR_DELIVERY', 'DELIVERED', 'EXCEPTION', 'RETURNED', 'CANCELLED')",
            name="canonical_status",
        ),
        CheckConstraint(
            "application_result IN ('APPLIED', 'NO_STATE_CHANGE', 'IGNORED_STALE', "
            "'IGNORED_INVALID_TRANSITION')",
            name="application_result",
        ),
        CheckConstraint(
            "previous_shipment_status IS NULL OR previous_shipment_status IN "
            "('PENDING', 'POSTED', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', 'DELIVERED', "
            "'EXCEPTION', 'RETURNED', 'CANCELLED')",
            name="previous_shipment_status",
        ),
        CheckConstraint(
            "resulting_shipment_status IN ('PENDING', 'POSTED', 'IN_TRANSIT', "
            "'OUT_FOR_DELIVERY', 'DELIVERED', 'EXCEPTION', 'RETURNED', 'CANCELLED')",
            name="resulting_shipment_status",
        ),
        CheckConstraint(
            "btrim(external_status) <> ''",
            name="external_status_nonempty",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    inbox_event_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "carrier_event_inbox.id",
            ondelete="RESTRICT",
            name="fk_tracking_events_inbox_event_id_carrier_event_inbox",
        ),
        nullable=False,
    )
    shipment_id: Mapped[UUID] = mapped_column(
        nullable=False,
    )
    carrier_id: Mapped[UUID] = mapped_column(
        nullable=False,
    )
    external_status: Mapped[str] = mapped_column(String(120), nullable=False)
    canonical_status: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    location: Mapped[str | None] = mapped_column(String(240), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    application_result: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_shipment_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resulting_shipment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index(
    "ix_tracking_events_shipment_occurred_at_created_at",
    TrackingEventModel.shipment_id,
    TrackingEventModel.occurred_at.desc(),
    TrackingEventModel.created_at.desc(),
)
