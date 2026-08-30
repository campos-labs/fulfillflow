"""Dedicated Pydantic schemas for the Orders HTTP boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from fulfillflow.orders.domain import Order, OrderStatus

ExternalReference = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
RecipientName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]
RecipientEmail = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=3, max_length=254),
]
PostalCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=16),
]
City = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]
StateCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]


@dataclass(frozen=True, slots=True)
class OrderFilters:
    """Public application filters supported by the Order list use case."""

    status: OrderStatus | None = None
    external_reference: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecipientCreate(_Schema):
    """Nested recipient input accepted on Order creation."""

    name: RecipientName
    email: RecipientEmail
    postal_code: PostalCode
    city: City
    state: StateCode

    @field_validator("email")
    @classmethod
    def validate_email_shape(cls, value: str) -> str:
        """Apply deterministic edge validation without another production package."""
        local, separator, domain = value.rpartition("@")
        if not separator or not local or "." not in domain or domain.startswith("."):
            raise ValueError("email must contain a local part and dotted domain")
        return value

    @field_validator("state", mode="before")
    @classmethod
    def normalize_state(cls, value: object) -> object:
        """Persist the documented uppercase state representation."""
        return value.strip().upper() if isinstance(value, str) else value


class OrderCreate(_Schema):
    """Order creation request."""

    external_reference: ExternalReference
    recipient: RecipientCreate


class RecipientRead(_Schema):
    """Recipient projection returned without exposing persistence names."""

    name: str
    email: str
    postal_code: str
    city: str
    state: str


class OrderRead(_Schema):
    """Order read response."""

    id: UUID
    external_reference: str
    recipient: RecipientRead
    status: OrderStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_order(cls, order: Order) -> Self:
        """Build the API projection from the framework-free entity."""
        return cls(
            id=order.id,
            external_reference=order.external_reference,
            recipient=RecipientRead(
                name=order.recipient_name,
                email=order.recipient_email,
                postal_code=order.recipient_postal_code,
                city=order.recipient_city,
                state=order.recipient_state,
            ),
            status=order.status,
            created_at=order.created_at,
            updated_at=order.updated_at,
        )


class OrderList(_Schema):
    """Paginated Order list response."""

    items: list[OrderRead]
    page: int
    page_size: int
    total: int
