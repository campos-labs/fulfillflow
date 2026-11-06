"""Authenticated Core HTTP DTOs, independent of either service implementation."""

import hashlib
import json
from datetime import UTC
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from fulfillflow.contracts.values import ShipmentApplicationResult, ShipmentStatus

EventId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[!-~]+$")]
ContentHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class CarrierRead(BaseModel):
    """Registry data required for reception and sanitized inbox queries."""

    model_config = ConfigDict(extra="forbid", from_attributes=True, frozen=True)
    id: UUID
    code: str
    name: str
    adapter_key: str
    active: bool


class ApplyEventCommand(BaseModel):
    """Stable identity and immutable normalized content of a received event."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: UUID
    carrier_id: UUID
    external_event_id: EventId
    payload_sha256: ContentHash
    tracking_code: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    canonical_status: Literal[
        "POSTED", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION", "RETURNED"
    ]
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    external_status: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    description: Annotated[str, StringConstraints(max_length=500)] | None
    location: Annotated[str, StringConstraints(max_length=240)] | None

    @field_validator("tracking_code")
    @classmethod
    def require_normalized_tracking_code(cls, value: str) -> str:
        if value != value.strip().upper():
            raise ValueError("tracking_code must be normalized")
        return value

    @field_validator("occurred_at", "received_at")
    @classmethod
    def normalize_instant(cls, value: AwareDatetime) -> AwareDatetime:
        return value.astimezone(UTC)

    def content_hash(self) -> str:
        """Bind identity, raw hash, canonical fields and original ordering data."""
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


class AppliedEventResult(BaseModel):
    """Original Core decision, sufficient for a later Tracking finalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["applied"] = "applied"
    event_id: UUID
    shipment_id: UUID
    result: ShipmentApplicationResult
    previous_status: ShipmentStatus
    current_status: ShipmentStatus
    decided_at: AwareDatetime


class RejectedEventResult(BaseModel):
    """Permanent domain rejection persisted even if the HTTP response is lost."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["rejected"] = "rejected"
    event_id: UUID
    code: Literal["SHIPMENT_NOT_FOUND_FOR_TRACKING"] = "SHIPMENT_NOT_FOUND_FOR_TRACKING"
    title: Literal["Shipment not found for tracking"] = "Shipment not found for tracking"
    detail: Literal["No Shipment matches the Carrier and tracking code."] = (
        "No Shipment matches the Carrier and tracking code."
    )
    decided_at: AwareDatetime


EventResult = Annotated[AppliedEventResult | RejectedEventResult, Field(discriminator="kind")]
