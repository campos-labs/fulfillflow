"""Carriers-owned lookup persistence; this module never commits."""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.carriers.models import CarrierModel


class CarrierRepository:
    """Resolve Carrier registry data without leaking ORM models."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_code(self, code: str, *, active_only: bool) -> CarrierModel | None:
        """Find one normalized Carrier code, optionally requiring active state."""
        statement = select(CarrierModel).where(CarrierModel.code == code)
        if active_only:
            statement = statement.where(CarrierModel.active.is_(True))
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def codes_by_ids(self, carrier_ids: Collection[UUID]) -> dict[UUID, str]:
        """Resolve codes in one query for Shipment read models."""
        if not carrier_ids:
            return {}
        rows = (
            await self._session.execute(
                select(CarrierModel.id, CarrierModel.code).where(
                    CarrierModel.id.in_(carrier_ids),
                )
            )
        ).all()
        return {row[0]: row[1] for row in rows}
