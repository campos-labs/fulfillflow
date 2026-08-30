"""Dedicated Pydantic schemas for the Shipments HTTP boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from fulfillflow.shipments.domain import Shipment, ShipmentStatus

CarrierCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=32,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
TrackingCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]


@dataclass(frozen=True, slots=True)
class ShipmentListFilters:
    """Public list filters before other modules resolve their owned keys."""

    status: ShipmentStatus | None = None
    carrier_code: str | None = None
    order_external_reference: str | None = None
    tracking_code: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


class _ShipmentView(Protocol):
    @property
    def shipment(self) -> Shipment: ...

    @property
    def carrier_code(self) -> str: ...


class _ShipmentSummary(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def carrier_code(self) -> str: ...

    @property
    def tracking_code(self) -> str: ...

    @property
    def status(self) -> ShipmentStatus: ...

    @property
    def estimated_delivery_date(self) -> date | None: ...


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShipmentCreate(_Schema):
    """Shipment creation request."""

    order_id: UUID
    carrier_code: CarrierCode
    tracking_code: TrackingCode
    estimated_delivery_date: date | None = None

    @field_validator("carrier_code", mode="before")
    @classmethod
    def normalize_carrier_code(cls, value: object) -> object:
        """Use the documented immutable lowercase slug representation."""
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("tracking_code", mode="before")
    @classmethod
    def normalize_tracking_code(cls, value: object) -> object:
        """Normalize tracking lookup values at the HTTP boundary."""
        return value.strip().upper() if isinstance(value, str) else value


class ShipmentRead(_Schema):
    """Operational Shipment response."""

    id: UUID
    order_id: UUID
    carrier_id: UUID
    carrier_code: str
    tracking_code: str
    status: ShipmentStatus
    status_occurred_at: datetime
    status_event_received_at: datetime | None
    status_external_event_id: str | None
    estimated_delivery_date: date | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: _ShipmentView) -> Self:
        """Build the API projection from public module data."""
        shipment = view.shipment
        return cls(
            id=shipment.id,
            order_id=shipment.order_id,
            carrier_id=shipment.carrier_id,
            carrier_code=view.carrier_code,
            tracking_code=shipment.tracking_code,
            status=shipment.status,
            status_occurred_at=shipment.status_occurred_at,
            status_event_received_at=shipment.status_event_received_at,
            status_external_event_id=shipment.status_external_event_id,
            estimated_delivery_date=shipment.estimated_delivery_date,
            shipped_at=shipment.shipped_at,
            delivered_at=shipment.delivered_at,
            created_at=shipment.created_at,
            updated_at=shipment.updated_at,
        )


class ShipmentSummaryRead(_Schema):
    """Shipment summary embedded in Order detail."""

    id: UUID
    carrier_code: str
    tracking_code: str
    status: ShipmentStatus
    estimated_delivery_date: date | None

    @classmethod
    def from_summary(cls, summary: _ShipmentSummary) -> Self:
        """Build an embedded summary from the public Shipments contract."""
        return cls(
            id=summary.id,
            carrier_code=summary.carrier_code,
            tracking_code=summary.tracking_code,
            status=summary.status,
            estimated_delivery_date=summary.estimated_delivery_date,
        )


class ShipmentList(_Schema):
    """Paginated Shipment list response."""

    items: list[ShipmentRead]
    page: int
    page_size: int
    total: int
