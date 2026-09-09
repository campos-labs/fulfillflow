"""Pydantic schemas exposed by the Tracking HTTP contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from fulfillflow.contracts.values import (
    InboxStatus,
    ShipmentApplicationResult,
    ShipmentStatus,
)


class WebhookResult(StrEnum):
    """Results returned by the synchronous carrier endpoint."""

    APPLIED = "APPLIED"
    NO_STATE_CHANGE = "NO_STATE_CHANGE"
    IGNORED_STALE = "IGNORED_STALE"
    IGNORED_INVALID_TRANSITION = "IGNORED_INVALID_TRANSITION"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, slots=True)
class CarrierEventFilters:
    """Public operational filters before Carrier codes are resolved."""

    carrier_code: str | None = None
    status: InboxStatus | None = None
    external_event_id: str | None = None
    received_from: datetime | None = None
    received_to: datetime | None = None


class _WebhookOutcome(Protocol):
    @property
    def external_event_id(self) -> str: ...

    @property
    def inbox_event_id(self) -> UUID: ...

    @property
    def tracking_event_id(self) -> UUID: ...

    @property
    def result(self) -> WebhookResult: ...

    @property
    def original_result(self) -> ShipmentApplicationResult | None: ...

    @property
    def shipment_id(self) -> UUID: ...

    @property
    def previous_status(self) -> ShipmentStatus | None: ...

    @property
    def current_status(self) -> ShipmentStatus: ...

    @property
    def request_id(self) -> UUID: ...


class _Inbox(Protocol):
    id: UUID
    carrier_id: UUID
    external_event_id: str
    received_at: datetime
    status: InboxStatus
    error_code: str | None
    error_detail: str | None
    processed_at: datetime | None
    request_id: UUID


class CarrierPayloadProjection(BaseModel):
    """Validated supplier fields allowed in the sanitized public projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    external_event_id: str
    tracking_code: str
    external_status: str
    occurred_at: datetime
    description: str | None
    location: str | None


class _CarrierEventView(Protocol):
    @property
    def inbox(self) -> _Inbox: ...

    @property
    def carrier_code(self) -> str: ...

    @property
    def tracking_event_id(self) -> UUID | None: ...

    @property
    def payload(self) -> CarrierPayloadProjection | None: ...


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WebhookResponse(_Schema):
    """Acknowledgement of one accepted or duplicate webhook."""

    external_event_id: str
    inbox_event_id: UUID
    tracking_event_id: UUID
    result: WebhookResult
    original_result: ShipmentApplicationResult | None = None
    shipment_id: UUID
    previous_status: ShipmentStatus | None
    current_status: ShipmentStatus
    request_id: UUID

    @classmethod
    def from_outcome(cls, outcome: _WebhookOutcome) -> Self:
        """Create an HTTP response without exposing application internals."""
        return cls(
            external_event_id=outcome.external_event_id,
            inbox_event_id=outcome.inbox_event_id,
            tracking_event_id=outcome.tracking_event_id,
            result=outcome.result,
            original_result=outcome.original_result,
            shipment_id=outcome.shipment_id,
            previous_status=outcome.previous_status,
            current_status=outcome.current_status,
            request_id=outcome.request_id,
        )


class TrackingEventRead(_Schema):
    """Canonical event exposed in a Shipment timeline."""

    id: UUID
    inbox_event_id: UUID
    shipment_id: UUID
    carrier_id: UUID
    external_status: str
    canonical_status: ShipmentStatus
    description: str | None
    location: str | None
    occurred_at: datetime
    received_at: datetime
    application_result: ShipmentApplicationResult
    previous_shipment_status: ShipmentStatus | None
    resulting_shipment_status: ShipmentStatus
    created_at: datetime

    @classmethod
    def from_event(cls, event: object) -> Self:
        """Project a framework-independent timeline entity."""
        return cls.model_validate(event, from_attributes=True)


class TrackingEventList(_Schema):
    """Stable paginated Shipment timeline."""

    items: list[TrackingEventRead]
    page: int
    page_size: int
    total: int


class CarrierEventSummaryRead(_Schema):
    """Sanitized Carrier inbox list item."""

    id: UUID
    carrier_id: UUID
    carrier_code: str
    external_event_id: str
    received_at: datetime
    status: InboxStatus
    error_code: str | None
    processed_at: datetime | None
    request_id: UUID
    tracking_event_id: UUID | None

    @classmethod
    def from_view(cls, view: _CarrierEventView) -> Self:
        """Project list-safe fields; raw bytes never leave this boundary."""
        inbox = view.inbox
        return cls(
            id=inbox.id,
            carrier_id=inbox.carrier_id,
            carrier_code=view.carrier_code,
            external_event_id=inbox.external_event_id,
            received_at=inbox.received_at,
            status=inbox.status,
            error_code=inbox.error_code,
            processed_at=inbox.processed_at,
            request_id=inbox.request_id,
            tracking_event_id=view.tracking_event_id,
        )


class CarrierEventRead(CarrierEventSummaryRead):
    """Sanitized detail with known validated fields, never forensic internals."""

    payload: CarrierPayloadProjection | None
    error_detail: str | None

    @classmethod
    def from_view(cls, view: _CarrierEventView) -> Self:
        """Project operational detail without signatures, secrets or raw bytes."""
        summary = CarrierEventSummaryRead.from_view(view)
        return cls(
            **summary.model_dump(),
            payload=view.payload,
            error_detail=view.inbox.error_detail,
        )


class CarrierEventList(_Schema):
    """Paginated operational inbox list."""

    items: list[CarrierEventSummaryRead]
    page: int
    page_size: int
    total: int
