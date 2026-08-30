"""SQLAlchemy model owned exclusively by the Carriers module."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, UniqueConstraint, true
from sqlalchemy.orm import Mapped, mapped_column

from fulfillflow.db.base import Base


class CarrierModel(Base):
    """Minimal persistent Carrier registry required for Shipment ownership."""

    __tablename__ = "carriers"
    __table_args__ = (
        UniqueConstraint("code", name="uq_carriers_code"),
        UniqueConstraint("adapter_key", name="uq_carriers_adapter_key"),
        CheckConstraint(
            "code ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="carrier_code_slug",
        ),
        CheckConstraint("btrim(name) <> ''", name="carrier_name_nonempty"),
        CheckConstraint("btrim(adapter_key) <> ''", name="adapter_key_nonempty"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    adapter_key: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
