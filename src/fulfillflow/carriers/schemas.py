"""Public schemas for simulated carrier payloads and their canonical projection."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

ExternalEventId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[!-~]+$"),
]
TrackingCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]
ExternalStatus = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]
Description = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
Location = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
]


class CanonicalShipmentStatus(StrEnum):
    """Carrier-normalized Shipment states that an external event may produce.

    Shipments owns its state machine and Carriers must not depend on that module.
    Tracking therefore converts this public value explicitly to the Shipments-owned
    enum before asking Shipments to apply the event.
    """

    POSTED = "POSTED"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    EXCEPTION = "EXCEPTION"
    RETURNED = "RETURNED"


class _ExternalCarrierSchema(BaseModel):
    """Validate known supplier fields while retaining tolerance for extensions."""

    model_config = ConfigDict(extra="allow")


class _CanonicalSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AlphaCarrierPayload(_ExternalCarrierSchema):
    """External JSON contract sent by Carrier Alpha."""

    event_id: ExternalEventId = Field(alias="eventId")
    tracking_code: TrackingCode = Field(alias="trackingCode")
    status: ExternalStatus
    event_date: AwareDatetime = Field(alias="eventDate")
    city: Location | None = None
    description: Description | None = None

    @field_validator("tracking_code", mode="before")
    @classmethod
    def normalize_tracking_code(cls, value: object) -> object:
        """Use the same canonical lookup representation as Shipments."""
        return value.strip().upper() if isinstance(value, str) else value


class BetaCarrierEvent(_ExternalCarrierSchema):
    """Nested event object in a Carrier Beta payload."""

    type: ExternalStatus
    occurred_at: AwareDatetime
    details: Description | None = None


class BetaCarrierLocation(_ExternalCarrierSchema):
    """Optional nested location object in a Carrier Beta payload."""

    city: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
        ]
        | None
    ) = None
    state: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=16),
        ]
        | None
    ) = None

    @field_validator("state", mode="before")
    @classmethod
    def normalize_state(cls, value: object) -> object:
        """Normalize state-like supplier values for the location projection."""
        return value.strip().upper() if isinstance(value, str) else value


class BetaCarrierPayload(_ExternalCarrierSchema):
    """External JSON contract sent by Carrier Beta."""

    id: ExternalEventId
    tracking_number: TrackingCode
    event: BetaCarrierEvent
    location: BetaCarrierLocation | None = None

    @field_validator("tracking_number", mode="before")
    @classmethod
    def normalize_tracking_number(cls, value: object) -> object:
        """Use the same canonical lookup representation as Shipments."""
        return value.strip().upper() if isinstance(value, str) else value


class CanonicalCarrierEvent(_CanonicalSchema):
    """Only representation returned by a carrier adapter."""

    external_event_id: ExternalEventId
    tracking_code: TrackingCode
    canonical_status: CanonicalShipmentStatus
    external_status: ExternalStatus
    occurred_at: AwareDatetime
    description: Description | None
    location: Location | None

    @field_validator("tracking_code", mode="before")
    @classmethod
    def normalize_tracking_code(cls, value: object) -> object:
        """Defend the canonical contract even when built outside an adapter."""
        return value.strip().upper() if isinstance(value, str) else value


class CarrierPayloadProjection(_CanonicalSchema):
    """Known validated external fields safe for operational API detail."""

    external_event_id: ExternalEventId
    tracking_code: TrackingCode
    external_status: ExternalStatus
    occurred_at: AwareDatetime
    description: Description | None
    location: Location | None
