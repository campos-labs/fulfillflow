"""Pure Carrier Alpha/Beta normalization through a static adapter allowlist."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar, Protocol

from pydantic import ValidationError

from fulfillflow.contracts.tracking import CarrierPayloadProjection
from fulfillflow.tracking.carrier_schemas import (
    AlphaCarrierPayload,
    BetaCarrierPayload,
    CanonicalCarrierEvent,
    CanonicalShipmentStatus,
)


class UnknownExternalStatusError(ValueError):
    """Raised when a valid carrier payload contains an unmapped status."""

    def __init__(self, adapter_key: str, external_status: str) -> None:
        self.adapter_key = adapter_key
        self.external_status = external_status
        super().__init__(f"Adapter '{adapter_key}' does not recognize the external status.")


class CarrierAdapterNotAllowedError(LookupError):
    """Raised when a persisted adapter key is absent from the code allowlist."""

    def __init__(self, adapter_key: str) -> None:
        self.adapter_key = adapter_key
        super().__init__(f"Carrier adapter '{adapter_key}' is not allowed.")


class CarrierAdapter(Protocol):
    """Public stateless adapter behavior consumed by Tracking."""

    @property
    def adapter_key(self) -> str:
        """Stable key stored in the Carrier registry."""
        ...

    @property
    def carrier_code(self) -> str:
        """Stable public carrier code represented by this adapter."""
        ...

    def normalize(self, payload: object) -> CanonicalCarrierEvent:
        """Validate one external payload and return its canonical projection."""
        ...


_ALPHA_STATUS_MAP: Mapping[str, CanonicalShipmentStatus] = MappingProxyType(
    {
        "CREATED": CanonicalShipmentStatus.POSTED,
        "MOVING": CanonicalShipmentStatus.IN_TRANSIT,
        "OUT_FOR_DELIVERY": CanonicalShipmentStatus.OUT_FOR_DELIVERY,
        "DELIVERED": CanonicalShipmentStatus.DELIVERED,
        "PROBLEM": CanonicalShipmentStatus.EXCEPTION,
        "RETURNED": CanonicalShipmentStatus.RETURNED,
    }
)

_BETA_STATUS_MAP: Mapping[str, CanonicalShipmentStatus] = MappingProxyType(
    {
        "label_created": CanonicalShipmentStatus.POSTED,
        "hub_scan": CanonicalShipmentStatus.IN_TRANSIT,
        "courier_route": CanonicalShipmentStatus.OUT_FOR_DELIVERY,
        "completed": CanonicalShipmentStatus.DELIVERED,
        "delivery_issue": CanonicalShipmentStatus.EXCEPTION,
        "returned_origin": CanonicalShipmentStatus.RETURNED,
    }
)


class AlphaCarrierAdapter:
    """Validate and normalize Carrier Alpha payloads without side effects."""

    adapter_key: ClassVar[str] = "alpha"
    carrier_code: ClassVar[str] = "carrier-alpha"

    def normalize(self, payload: object) -> CanonicalCarrierEvent:
        external = AlphaCarrierPayload.model_validate(payload)
        canonical_status = _map_status(
            _ALPHA_STATUS_MAP,
            adapter_key=self.adapter_key,
            external_status=external.status,
        )
        return CanonicalCarrierEvent(
            external_event_id=external.event_id,
            tracking_code=external.tracking_code,
            canonical_status=canonical_status,
            external_status=external.status,
            occurred_at=external.event_date,
            description=external.description,
            location=external.city,
        )


class BetaCarrierAdapter:
    """Validate and normalize Carrier Beta payloads without side effects."""

    adapter_key: ClassVar[str] = "beta"
    carrier_code: ClassVar[str] = "carrier-beta"

    def normalize(self, payload: object) -> CanonicalCarrierEvent:
        external = BetaCarrierPayload.model_validate(payload)
        canonical_status = _map_status(
            _BETA_STATUS_MAP,
            adapter_key=self.adapter_key,
            external_status=external.event.type,
        )
        return CanonicalCarrierEvent(
            external_event_id=external.id,
            tracking_code=external.tracking_number,
            canonical_status=canonical_status,
            external_status=external.event.type,
            occurred_at=external.event.occurred_at,
            description=external.event.details,
            location=_beta_location(external),
        )


_ADAPTER_ALLOWLIST: Mapping[str, CarrierAdapter] = MappingProxyType(
    {
        "alpha": AlphaCarrierAdapter(),
        "beta": BetaCarrierAdapter(),
    }
)


def resolve_carrier_adapter(adapter_key: str) -> CarrierAdapter:
    """Resolve only explicitly constructed adapters; never import from data."""
    try:
        return _ADAPTER_ALLOWLIST[adapter_key]
    except KeyError as exc:
        raise CarrierAdapterNotAllowedError(adapter_key) from exc


def normalize_carrier_event(adapter_key: str, payload: object) -> CanonicalCarrierEvent:
    """Resolve the allowlisted adapter and normalize one parsed JSON value."""
    return resolve_carrier_adapter(adapter_key).normalize(payload)


def project_known_carrier_payload(
    adapter_key: str,
    payload: object,
) -> CarrierPayloadProjection | None:
    """Return only validated known supplier fields, never allowed extensions."""
    adapter = resolve_carrier_adapter(adapter_key)
    try:
        if isinstance(adapter, AlphaCarrierAdapter):
            alpha_external = AlphaCarrierPayload.model_validate(payload)
            return CarrierPayloadProjection(
                external_event_id=alpha_external.event_id,
                tracking_code=alpha_external.tracking_code,
                external_status=alpha_external.status,
                occurred_at=alpha_external.event_date,
                description=alpha_external.description,
                location=alpha_external.city,
            )
        beta_external = BetaCarrierPayload.model_validate(payload)
        return CarrierPayloadProjection(
            external_event_id=beta_external.id,
            tracking_code=beta_external.tracking_number,
            external_status=beta_external.event.type,
            occurred_at=beta_external.event.occurred_at,
            description=beta_external.event.details,
            location=_beta_location(beta_external),
        )
    except ValidationError:
        return None


def supported_adapter_keys() -> frozenset[str]:
    """Return an immutable view of configured adapter keys."""
    return frozenset(_ADAPTER_ALLOWLIST)


def _map_status(
    status_map: Mapping[str, CanonicalShipmentStatus],
    *,
    adapter_key: str,
    external_status: str,
) -> CanonicalShipmentStatus:
    try:
        return status_map[external_status]
    except KeyError as exc:
        raise UnknownExternalStatusError(adapter_key, external_status) from exc


def _beta_location(payload: BetaCarrierPayload) -> str | None:
    location = payload.location
    if location is None:
        return None
    parts = [part for part in (location.city, location.state) if part is not None]
    return ", ".join(parts) or None
