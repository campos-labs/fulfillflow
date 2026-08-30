"""Shipments-owned persistence operations; this module never commits."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.shared import Page
from fulfillflow.shipments.domain import Shipment, ShipmentStatus
from fulfillflow.shipments.models import ShipmentModel


@dataclass(frozen=True, slots=True)
class ShipmentFilters:
    """Resolved Shipment-table filters; other modules resolve their own keys."""

    status: ShipmentStatus | None = None
    carrier_id: UUID | None = None
    order_id: UUID | None = None
    tracking_code: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


class ShipmentRepository:
    """Map Shipment entities to their module-owned SQLAlchemy records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, shipment: Shipment) -> None:
        """Stage and flush a new Shipment without committing."""
        self._session.add(_to_model(shipment))
        await self._session.flush()

    async def get(
        self,
        shipment_id: UUID,
        *,
        for_update: bool = False,
    ) -> Shipment | None:
        """Load one Shipment, optionally with its pessimistic row lock."""
        statement = select(ShipmentModel).where(ShipmentModel.id == shipment_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        return _to_entity(model) if model is not None else None

    async def save(self, shipment: Shipment) -> None:
        """Persist all state-machine fields and flush without committing."""
        model = await self._session.get(ShipmentModel, shipment.id)
        if model is None:
            raise LookupError(f"Shipment {shipment.id} disappeared during its transaction")
        model.status = shipment.status.value
        model.status_occurred_at = shipment.status_occurred_at
        model.status_event_received_at = shipment.status_event_received_at
        model.status_external_event_id = shipment.status_external_event_id
        model.shipped_at = shipment.shipped_at
        model.delivered_at = shipment.delivered_at
        model.updated_at = shipment.updated_at
        await self._session.flush()

    async def list(
        self,
        filters: ShipmentFilters,
        *,
        page: int,
        page_size: int,
    ) -> Page[Shipment]:
        """Return a stable newest-first page and matching total."""
        predicates = []
        if filters.status is not None:
            predicates.append(ShipmentModel.status == filters.status.value)
        if filters.carrier_id is not None:
            predicates.append(ShipmentModel.carrier_id == filters.carrier_id)
        if filters.order_id is not None:
            predicates.append(ShipmentModel.order_id == filters.order_id)
        if filters.tracking_code is not None:
            predicates.append(ShipmentModel.tracking_code == filters.tracking_code)
        if filters.created_from is not None:
            predicates.append(ShipmentModel.created_at >= filters.created_from)
        if filters.created_to is not None:
            predicates.append(ShipmentModel.created_at <= filters.created_to)

        total = await self._session.scalar(
            select(func.count()).select_from(ShipmentModel).where(*predicates)
        )
        statement = (
            select(ShipmentModel)
            .where(*predicates)
            .order_by(ShipmentModel.created_at.desc(), ShipmentModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        models = list((await self._session.scalars(statement)).all())
        return Page(
            items=[_to_entity(model) for model in models],
            page=page,
            page_size=page_size,
            total=total or 0,
        )

    async def list_for_order(self, order_id: UUID) -> builtins.list[Shipment]:
        """Return all Shipment summaries for one Order in stable order."""
        statement = (
            select(ShipmentModel)
            .where(ShipmentModel.order_id == order_id)
            .order_by(ShipmentModel.created_at.asc(), ShipmentModel.id.asc())
        )
        return [_to_entity(model) for model in (await self._session.scalars(statement)).all()]

    async def completion_counts(self, order_id: UUID) -> tuple[int, int]:
        """Count non-cancelled and undelivered Shipments using only owned rows."""
        non_cancelled = ShipmentModel.status != ShipmentStatus.CANCELLED.value
        total = await self._session.scalar(
            select(func.count())
            .select_from(ShipmentModel)
            .where(ShipmentModel.order_id == order_id, non_cancelled)
        )
        undelivered = await self._session.scalar(
            select(func.count())
            .select_from(ShipmentModel)
            .where(
                ShipmentModel.order_id == order_id,
                non_cancelled,
                ShipmentModel.status != ShipmentStatus.DELIVERED.value,
            )
        )
        return total or 0, undelivered or 0


def _to_model(shipment: Shipment) -> ShipmentModel:
    return ShipmentModel(
        id=shipment.id,
        order_id=shipment.order_id,
        carrier_id=shipment.carrier_id,
        tracking_code=shipment.tracking_code,
        status=shipment.status.value,
        status_occurred_at=shipment.status_occurred_at,
        status_event_received_at=shipment.status_event_received_at,
        status_external_event_id=shipment.status_external_event_id,
        estimated_delivery_date=shipment.estimated_delivery_date,
        shipped_at=shipment.shipped_at,
        delivered_at=shipment.delivered_at,
        created_at=shipment.created_at,
        updated_at=shipment.updated_at,
    )


def _to_entity(model: ShipmentModel) -> Shipment:
    return Shipment(
        id=model.id,
        order_id=model.order_id,
        carrier_id=model.carrier_id,
        tracking_code=model.tracking_code,
        status=ShipmentStatus(model.status),
        status_occurred_at=model.status_occurred_at,
        status_event_received_at=model.status_event_received_at,
        status_external_event_id=model.status_external_event_id,
        estimated_delivery_date=model.estimated_delivery_date,
        shipped_at=model.shipped_at,
        delivered_at=model.delivered_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )
