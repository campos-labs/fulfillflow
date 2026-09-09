"""Narrow authenticated registry and event-application endpoints in Core."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.carriers.public import CarriersPublic
from fulfillflow.contracts.core import ApplyEventCommand, CarrierRead, EventResult
from fulfillflow.core.events import CoreEventService
from fulfillflow.http.dependencies import get_clock, get_session
from fulfillflow.shared import Clock
from fulfillflow.shipments.public import ShipmentNotFoundError, ShipmentsPublic

router = APIRouter(prefix="/internal/v1", include_in_schema=False)
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
ClockDependency = Annotated[Clock, Depends(get_clock)]


@router.get("/carriers", response_model=list[CarrierRead])
async def get_carriers(
    session: SessionDependency,
    ids: Annotated[list[UUID] | None, Query(max_length=100)] = None,
) -> list[CarrierRead]:
    async with session.begin():
        carriers = await CarriersPublic(session).views_by_ids(set(ids or []))
    return [CarrierRead.model_validate(carrier) for carrier in carriers.values()]


@router.get("/carriers/{code}", response_model=CarrierRead | None)
async def get_carrier(
    code: str, session: SessionDependency, active_only: bool = False
) -> CarrierRead | None:
    async with session.begin():
        registry = CarriersPublic(session)
        carrier = await registry.require_active(code) if active_only else await registry.find(code)
    return CarrierRead.model_validate(carrier) if carrier is not None else None


@router.get("/shipments/{shipment_id}", response_model=UUID)
async def require_shipment(shipment_id: UUID, session: SessionDependency) -> UUID:
    async with session.begin():
        if await ShipmentsPublic(session).find(shipment_id) is None:
            raise ShipmentNotFoundError(shipment_id)
    return shipment_id


@router.post("/tracking-events", response_model=EventResult)
async def apply_tracking_event(
    command: ApplyEventCommand, session: SessionDependency, clock: ClockDependency
) -> EventResult:
    return await CoreEventService(session, clock).apply(command)
