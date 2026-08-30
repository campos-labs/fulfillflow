"""Explicit public Carrier lookup interface used by Shipments."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.carriers.repository import CarrierRepository


@dataclass(frozen=True, slots=True)
class CarrierView:
    """Minimal immutable Carrier data visible to another module."""

    id: UUID
    code: str
    name: str
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
        return CarrierView(model.id, model.code, model.name, model.active)

    async def find(self, code: str) -> CarrierView | None:
        """Find a Carrier regardless of active state for operational filtering."""
        model = await self._repository.find_by_code(code, active_only=False)
        if model is None:
            return None
        return CarrierView(model.id, model.code, model.name, model.active)

    async def codes_by_ids(self, carrier_ids: set[UUID]) -> dict[UUID, str]:
        """Resolve Carrier codes for a Shipment page without cross-module joins."""
        return await self._repository.codes_by_ids(carrier_ids)
