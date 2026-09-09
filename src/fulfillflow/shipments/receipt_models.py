"""Core-local event receipt, committed atomically with Shipment effects."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fulfillflow.db.base import Base


class EventReceiptModel(Base):
    __tablename__ = "tracking_event_receipts"
    __table_args__ = (
        UniqueConstraint(
            "carrier_id", "external_event_id", name="uq_tracking_receipts_carrier_event"
        ),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="receipt_hash_lower_hex"),
        CheckConstraint("btrim(external_event_id) <> ''", name="receipt_external_event_nonempty"),
    )

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "carriers.id", ondelete="RESTRICT", name="fk_tracking_receipts_carrier_id_carriers"
        )
    )
    external_event_id: Mapped[str] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(CHAR(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
