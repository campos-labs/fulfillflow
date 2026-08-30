"""Explicit public Carrier lookup and adapter contracts used by other modules."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.carriers.adapters import (
    AlphaCarrierAdapter,
    BetaCarrierAdapter,
    CarrierAdapter,
    CarrierAdapterNotAllowedError,
    UnknownExternalStatusError,
    normalize_carrier_event,
    project_known_carrier_payload,
    resolve_carrier_adapter,
    supported_adapter_keys,
)
from fulfillflow.carriers.repository import CarrierRepository
from fulfillflow.carriers.schemas import (
    AlphaCarrierPayload,
    BetaCarrierEvent,
    BetaCarrierLocation,
    BetaCarrierPayload,
    CanonicalCarrierEvent,
    CanonicalShipmentStatus,
    CarrierPayloadProjection,
)


@dataclass(frozen=True, slots=True)
class CarrierView:
    """Minimal immutable Carrier data visible to another module."""

    id: UUID
    code: str
    name: str
    adapter_key: str
    active: bool


class CarrierNotFoundError(LookupError):
    """Raised when Shipment creation names no active Carrier."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Active Carrier '{code}' was not found.")


class CarriersPublic:
    """Transaction-participating Carrier facade; it never owns commit/rollback."""

    def __init__(self, session: AsyncSession) -> None:
        self._repository = CarrierRepository(session)

    async def require_active(self, code: str) -> CarrierView:
        """Return an active Carrier or raise a sanitized not-found error."""
        model = await self._repository.find_by_code(code, active_only=True)
        if model is None:
            raise CarrierNotFoundError(code)
        return CarrierView(model.id, model.code, model.name, model.adapter_key, model.active)

    async def find(self, code: str) -> CarrierView | None:
        """Find a Carrier regardless of active state for operational filtering."""
        model = await self._repository.find_by_code(code, active_only=False)
        if model is None:
            return None
        return CarrierView(model.id, model.code, model.name, model.adapter_key, model.active)

    async def codes_by_ids(self, carrier_ids: set[UUID]) -> dict[UUID, str]:
        """Resolve Carrier codes for a Shipment page without cross-module joins."""
        return await self._repository.codes_by_ids(carrier_ids)

    async def views_by_ids(self, carrier_ids: set[UUID]) -> dict[UUID, CarrierView]:
        """Resolve immutable registry views for public cross-module projections."""
        models = await self._repository.find_by_ids(carrier_ids)
        return {
            model.id: CarrierView(
                model.id,
                model.code,
                model.name,
                model.adapter_key,
                model.active,
            )
            for model in models
        }


__all__ = [
    "AlphaCarrierAdapter",
    "AlphaCarrierPayload",
    "BetaCarrierAdapter",
    "BetaCarrierEvent",
    "BetaCarrierLocation",
    "BetaCarrierPayload",
    "CanonicalCarrierEvent",
    "CanonicalShipmentStatus",
    "CarrierAdapter",
    "CarrierAdapterNotAllowedError",
    "CarrierNotFoundError",
    "CarrierPayloadProjection",
    "CarrierView",
    "CarriersPublic",
    "UnknownExternalStatusError",
    "normalize_carrier_event",
    "project_known_carrier_payload",
    "resolve_carrier_adapter",
    "supported_adapter_keys",
]
