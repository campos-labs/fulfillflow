"""Framework-independent Tracking entities and persisted value types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class InboxStatus(StrEnum):
    """Lifecycle of one authenticated Carrier webhook."""

    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"


class ShipmentStatus(StrEnum):
    """Shipment status values copied into the immutable Tracking timeline.

    Tracking owns this persisted representation. Conversion to and from the
    Shipments public enum happens in the application service boundary.
    """

    PENDING = "PENDING"
    POSTED = "POSTED"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    EXCEPTION = "EXCEPTION"
    RETURNED = "RETURNED"
    CANCELLED = "CANCELLED"


class ShipmentApplicationResult(StrEnum):
    """Result persisted for application of one canonical event."""

    APPLIED = "APPLIED"
    NO_STATE_CHANGE = "NO_STATE_CHANGE"
    IGNORED_STALE = "IGNORED_STALE"
    IGNORED_INVALID_TRANSITION = "IGNORED_INVALID_TRANSITION"


@dataclass(slots=True)
class CarrierEventInbox:
    """Authenticated payload preserved independently from business processing."""

    id: UUID
    carrier_id: UUID
    external_event_id: str
    payload_sha256: str
    raw_body: bytes
    parsed_payload: JsonValue
    received_at: datetime
    status: InboxStatus
    error_code: str | None
    error_detail: str | None
    processed_at: datetime | None
    request_id: UUID

    def mark_processed(self, processed_at: datetime) -> None:
        """Finalize a successfully normalized event."""
        self.status = InboxStatus.PROCESSED
        self.error_code = None
        self.error_detail = None
        self.processed_at = processed_at

    def mark_rejected(
        self,
        *,
        code: str,
        detail: str,
        processed_at: datetime,
        parsed_payload: JsonValue,
    ) -> None:
        """Finalize a permanent supplier or domain rejection."""
        self.parsed_payload = parsed_payload
        self.status = InboxStatus.REJECTED
        self.error_code = code
        self.error_detail = detail
        self.processed_at = processed_at


@dataclass(frozen=True, slots=True)
class TrackingEvent:
    """Append-only canonical timeline record."""

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
