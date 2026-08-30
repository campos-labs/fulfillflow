"""Shipment application services and cross-module transaction coordination."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.carriers.public import CarriersPublic
from fulfillflow.orders.public import OrdersPublic
from fulfillflow.shared import Clock, Page, new_uuid
from fulfillflow.shipments.domain import (
    Shipment,
    ShipmentApplicationResult,
    ShipmentStatus,
    ShipmentTransition,
)
from fulfillflow.shipments.repository import ShipmentFilters, ShipmentRepository
from fulfillflow.shipments.schemas import ShipmentListFilters


@dataclass(frozen=True, slots=True)
class CreateShipmentCommand:
    """Validated values required to create a Shipment."""

    order_id: UUID
    carrier_code: str
    tracking_code: str
    estimated_delivery_date: date | None


@dataclass(frozen=True, slots=True)
class ShipmentView:
    """Operational Shipment read model with a public Carrier code."""

    shipment: Shipment
    carrier_code: str


@dataclass(frozen=True, slots=True)
class ShipmentSummary:
    """Shipment fields included in an Order detail response."""

    id: UUID
    carrier_code: str
    tracking_code: str
    status: ShipmentStatus
    estimated_delivery_date: date | None


@dataclass(frozen=True, slots=True)
class AppliedShipmentTransition:
    """Result exposed to a future Tracking coordinator without tracking storage."""

    transition: ShipmentTransition
    shipment: Shipment
    order_completed: bool


class ShipmentNotFoundError(LookupError):
    """Raised when a Shipment identifier does not exist."""

    def __init__(self, shipment_id: UUID) -> None:
        self.shipment_id = shipment_id
        super().__init__(f"Shipment {shipment_id} was not found.")


class ShipmentTrackingCodeConflictError(ValueError):
    """Raised when database uniqueness rejects a Carrier tracking code."""

    def __init__(self, carrier_code: str, tracking_code: str) -> None:
        self.carrier_code = carrier_code
        self.tracking_code = tracking_code
        super().__init__(
            f"Tracking code '{tracking_code}' already exists for Carrier '{carrier_code}'."
        )


class ShipmentService:
    """Own transactions and coordinate only through Orders/Carriers public facades."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._repository = ShipmentRepository(session)
        self._orders = OrdersPublic(session)
        self._carriers = CarriersPublic(session)

    async def create(self, command: CreateShipmentCommand) -> ShipmentView:
        """Create PENDING Shipment while locking its confirmed Order."""
        carrier_code = command.carrier_code.strip().lower()
        tracking_code = command.tracking_code.strip().upper()
        occurred_at = self._clock.now()
        try:
            async with self._session.begin():
                order = await self._orders.require_confirmed_for_shipment(
                    command.order_id,
                    for_update=True,
                )
                carrier = await self._carriers.require_active(carrier_code)
                shipment = Shipment(
                    id=new_uuid(),
                    order_id=order.id,
                    carrier_id=carrier.id,
                    tracking_code=tracking_code,
                    status=ShipmentStatus.PENDING,
                    status_occurred_at=occurred_at,
                    status_event_received_at=None,
                    status_external_event_id=None,
                    estimated_delivery_date=command.estimated_delivery_date,
                    shipped_at=None,
                    delivered_at=None,
                    created_at=occurred_at,
                    updated_at=occurred_at,
                )
                await self._repository.add(shipment)
        except IntegrityError as exc:
            raise ShipmentTrackingCodeConflictError(carrier_code, tracking_code) from exc
        return ShipmentView(shipment, carrier.code)

    async def get(self, shipment_id: UUID) -> ShipmentView:
        """Read one Shipment and resolve its Carrier via the public contract."""
        async with self._session.begin():
            shipment = await self._repository.get(shipment_id)
            if shipment is None:
                raise ShipmentNotFoundError(shipment_id)
            codes = await self._carriers.codes_by_ids({shipment.carrier_id})
        return ShipmentView(shipment, codes[shipment.carrier_id])

    async def list(
        self,
        filters: ShipmentListFilters,
        *,
        page: int,
        page_size: int,
    ) -> Page[ShipmentView]:
        """Resolve foreign filters publicly and query only Shipment-owned rows."""
        async with self._session.begin():
            carrier_id: UUID | None = None
            if filters.carrier_code is not None:
                carrier = await self._carriers.find(filters.carrier_code.strip().lower())
                if carrier is None:
                    return Page([], page, page_size, 0)
                carrier_id = carrier.id

            order_id: UUID | None = None
            if filters.order_external_reference is not None:
                order_id = await self._orders.find_id_by_external_reference(
                    filters.order_external_reference.strip()
                )
                if order_id is None:
                    return Page([], page, page_size, 0)

            result = await self._repository.list(
                ShipmentFilters(
                    status=filters.status,
                    carrier_id=carrier_id,
                    order_id=order_id,
                    tracking_code=(
                        filters.tracking_code.strip().upper()
                        if filters.tracking_code is not None
                        else None
                    ),
                    created_from=filters.created_from,
                    created_to=filters.created_to,
                ),
                page=page,
                page_size=page_size,
            )
            codes = await self._carriers.codes_by_ids(
                {shipment.carrier_id for shipment in result.items}
            )
        return Page(
            [ShipmentView(shipment, codes[shipment.carrier_id]) for shipment in result.items],
            result.page,
            result.page_size,
            result.total,
        )

    async def cancel(self, shipment_id: UUID) -> ShipmentView:
        """Lock and idempotently cancel a manually cancellable Shipment."""
        async with self._session.begin():
            shipment = await self._require_locked(shipment_id)
            occurred_at = self._clock.now()
            if shipment.cancel(occurred_at):
                await self._repository.save(shipment)
                await self._complete_order_if_eligible(shipment, occurred_at)
            codes = await self._carriers.codes_by_ids({shipment.carrier_id})
        return ShipmentView(shipment, codes[shipment.carrier_id])

    async def apply_tracking_status(
        self,
        shipment_id: UUID,
        target: ShipmentStatus,
        *,
        occurred_at: datetime,
        received_at: datetime,
        external_event_id: str,
    ) -> AppliedShipmentTransition:
        """Apply canonical state under Shipment→Order lock order in one transaction.

        This is a public business interface for a later Tracking increment. It does
        not authenticate webhooks or create inbox/timeline/notification records.
        """
        async with self._session.begin():
            shipment = await self._require_locked(shipment_id)
            transition = shipment.apply_external_status(
                target,
                occurred_at=occurred_at,
                received_at=received_at,
                external_event_id=external_event_id,
            )
            order_completed = False
            if transition.result in {
                ShipmentApplicationResult.APPLIED,
                ShipmentApplicationResult.NO_STATE_CHANGE,
            }:
                await self._repository.save(shipment)

            if transition.result is ShipmentApplicationResult.APPLIED and shipment.status in {
                ShipmentStatus.DELIVERED,
                ShipmentStatus.CANCELLED,
            }:
                order_completed = await self._complete_order_if_eligible(shipment, received_at)

        return AppliedShipmentTransition(transition, shipment, order_completed)

    async def _require_locked(self, shipment_id: UUID) -> Shipment:
        shipment = await self._repository.get(shipment_id, for_update=True)
        if shipment is None:
            raise ShipmentNotFoundError(shipment_id)
        return shipment

    async def _complete_order_if_eligible(
        self,
        shipment: Shipment,
        occurred_at: datetime,
    ) -> bool:
        """Evaluate completion after holding the Shipment lock, then the Order lock."""
        order = await self._orders.lock_for_completion(shipment.order_id)
        total, undelivered = await self._repository.completion_counts(shipment.order_id)
        if total == 0 or undelivered != 0:
            return False
        return await self._orders.complete_locked(order, occurred_at)


class ShipmentsPublic:
    """Transaction-participating Shipment operations used by other modules."""

    def __init__(self, session: AsyncSession) -> None:
        self._repository = ShipmentRepository(session)
        self._carriers = CarriersPublic(session)
        self._orders = OrdersPublic(session)

    async def find(self, shipment_id: UUID) -> Shipment | None:
        """Return a Shipment for a caller-owned read transaction."""
        return await self._repository.get(shipment_id)

    async def lock_for_tracking(
        self,
        carrier_id: UUID,
        tracking_code: str,
    ) -> Shipment | None:
        """Acquire the Shipment lock after the Tracking inbox lock."""
        return await self._repository.get_by_carrier_tracking_code(
            carrier_id,
            tracking_code.strip().upper(),
            for_update=True,
        )

    async def apply_tracking_status_locked(
        self,
        shipment: Shipment,
        target: ShipmentStatus,
        *,
        occurred_at: datetime,
        received_at: datetime,
        external_event_id: str,
    ) -> AppliedShipmentTransition:
        """Apply an event while participating in the caller's transaction.

        The caller must already hold the Shipment lock. Any Order lock is acquired
        only after it, preserving the global inbox -> Shipment -> Order order.
        """
        transition = shipment.apply_external_status(
            target,
            occurred_at=occurred_at,
            received_at=received_at,
            external_event_id=external_event_id,
        )
        if transition.result in {
            ShipmentApplicationResult.APPLIED,
            ShipmentApplicationResult.NO_STATE_CHANGE,
        }:
            await self._repository.save(shipment)

        order_completed = False
        if transition.result is ShipmentApplicationResult.APPLIED and shipment.status in {
            ShipmentStatus.DELIVERED,
            ShipmentStatus.CANCELLED,
        }:
            order = await self._orders.lock_for_completion(shipment.order_id)
            total, undelivered = await self._repository.completion_counts(shipment.order_id)
            if total != 0 and undelivered == 0:
                order_completed = await self._orders.complete_locked(order, received_at)

        return AppliedShipmentTransition(transition, shipment, order_completed)

    async def summaries_for_order(self, order_id: UUID) -> builtins.list[ShipmentSummary]:
        """Return Shipment-owned summaries inside the caller's transaction."""
        shipments = await self._repository.list_for_order(order_id)
        codes = await self._carriers.codes_by_ids({shipment.carrier_id for shipment in shipments})
        return [
            ShipmentSummary(
                id=shipment.id,
                carrier_code=codes[shipment.carrier_id],
                tracking_code=shipment.tracking_code,
                status=shipment.status,
                estimated_delivery_date=shipment.estimated_delivery_date,
            )
            for shipment in shipments
        ]
