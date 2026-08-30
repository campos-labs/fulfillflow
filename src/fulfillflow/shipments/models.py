"""SQLAlchemy model owned exclusively by the Shipments module."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from fulfillflow.db.base import Base


class ShipmentModel(Base):
    """Physical Shipment record; relationships stay behind public module contracts."""

    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint(
            "carrier_id",
            "tracking_code",
            name="uq_shipments_carrier_tracking_code",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'POSTED', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', "
            "'DELIVERED', 'EXCEPTION', 'RETURNED', 'CANCELLED')",
            name="shipment_status",
        ),
        CheckConstraint(
            "tracking_code = upper(btrim(tracking_code)) AND tracking_code <> ''",
            name="tracking_code_normalized",
        ),
        CheckConstraint(
            "(status_event_received_at IS NULL AND status_external_event_id IS NULL) OR "
            "(status_event_received_at IS NOT NULL AND status_external_event_id IS NOT NULL)",
            name="external_ordering_complete",
        ),
        CheckConstraint(
            "status_external_event_id IS NULL OR status_external_event_id <> ''",
            name="external_event_id_nonempty",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT", name="fk_shipments_order_id_orders"),
        nullable=False,
    )
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="RESTRICT", name="fk_shipments_carrier_id_carriers"),
        nullable=False,
    )
    tracking_code: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    status_event_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status_external_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    estimated_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index("ix_shipments_order_id", ShipmentModel.order_id)
Index(
    "ix_shipments_status_updated_at",
    ShipmentModel.status,
    ShipmentModel.updated_at.desc(),
)
