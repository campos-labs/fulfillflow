"""SQLAlchemy model owned exclusively by the Orders module."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fulfillflow.db.base import Base


class OrderModel(Base):
    """Physical Order record; API and public contracts never expose this type."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("external_reference", name="uq_orders_external_reference"),
        CheckConstraint(
            "status IN ('CREATED', 'CONFIRMED', 'FULFILLED', 'CANCELLED')",
            name="order_status",
        ),
        CheckConstraint("btrim(external_reference) <> ''", name="external_reference_nonempty"),
        CheckConstraint("btrim(recipient_name) <> ''", name="recipient_name_nonempty"),
        CheckConstraint("btrim(recipient_email) <> ''", name="recipient_email_nonempty"),
        CheckConstraint(
            "btrim(recipient_postal_code) <> ''",
            name="recipient_postal_code_nonempty",
        ),
        CheckConstraint("btrim(recipient_city) <> ''", name="recipient_city_nonempty"),
        CheckConstraint(
            "recipient_state = upper(recipient_state) "
            "AND char_length(recipient_state) = 2 AND btrim(recipient_state) <> ''",
            name="recipient_state_upper",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    external_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_name: Mapped[str] = mapped_column(String(160), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(254), nullable=False)
    recipient_postal_code: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_city: Mapped[str] = mapped_column(String(120), nullable=False)
    recipient_state: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index(
    "ix_orders_status_created_at",
    OrderModel.status,
    OrderModel.created_at.desc(),
)
