"""Order application services and transaction coordination."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.orders.domain import Order, OrderStatus
from fulfillflow.orders.repository import OrderRepository
from fulfillflow.orders.schemas import OrderFilters
from fulfillflow.shared import Clock, Page, new_uuid


@dataclass(frozen=True, slots=True)
class CreateOrderCommand:
    """Validated values required to create an Order."""

    external_reference: str
    recipient_name: str
    recipient_email: str
    recipient_postal_code: str
    recipient_city: str
    recipient_state: str


class OrderNotFoundError(LookupError):
    """Raised when an Order identifier does not exist."""

    def __init__(self, order_id: UUID) -> None:
        self.order_id = order_id
        super().__init__(f"Order {order_id} was not found.")


class OrderExternalReferenceConflictError(ValueError):
    """Raised when the database uniqueness authority rejects a reference."""

    def __init__(self, external_reference: str) -> None:
        self.external_reference = external_reference
        super().__init__(f"Order reference '{external_reference}' already exists.")


class OrderService:
    """Own transaction boundaries for API-facing Order operations."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._repository = OrderRepository(session)

    async def create(self, command: CreateOrderCommand) -> Order:
        """Create an Order in CREATED state in one explicit transaction."""
        occurred_at = self._clock.now()
        order = Order(
            id=new_uuid(),
            external_reference=command.external_reference.strip(),
            recipient_name=command.recipient_name.strip(),
            recipient_email=command.recipient_email.strip(),
            recipient_postal_code=command.recipient_postal_code.strip(),
            recipient_city=command.recipient_city.strip(),
            recipient_state=command.recipient_state.strip().upper(),
            status=OrderStatus.CREATED,
            created_at=occurred_at,
            updated_at=occurred_at,
        )
        try:
            async with self._session.begin():
                await self._repository.add(order)
        except IntegrityError as exc:
            raise OrderExternalReferenceConflictError(order.external_reference) from exc
        return order

    async def get(self, order_id: UUID) -> Order:
        """Read one Order in an explicit read transaction."""
        async with self._session.begin():
            order = await self._repository.get(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    async def list(
        self,
        filters: OrderFilters,
        *,
        page: int,
        page_size: int,
    ) -> Page[Order]:
        """Read a stable filtered Order page."""
        async with self._session.begin():
            return await self._repository.list(
                status=filters.status,
                external_reference=filters.external_reference,
                created_from=filters.created_from,
                created_to=filters.created_to,
                page=page,
                page_size=page_size,
            )

    async def confirm(self, order_id: UUID) -> Order:
        """Lock and idempotently confirm an Order."""
        async with self._session.begin():
            order = await self._require_locked(order_id)
            if order.confirm(self._clock.now()):
                await self._repository.save(order)
        return order

    async def cancel(self, order_id: UUID) -> Order:
        """Lock and idempotently cancel an Order."""
        async with self._session.begin():
            order = await self._require_locked(order_id)
            if order.cancel(self._clock.now()):
                await self._repository.save(order)
        return order

    async def _require_locked(self, order_id: UUID) -> Order:
        order = await self._repository.get(order_id, for_update=True)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order


@dataclass(frozen=True, slots=True)
class OrderForShipment:
    """Minimum Order data disclosed to the Shipments module."""

    id: UUID
    external_reference: str
    recipient_email: str
    status: OrderStatus


class OrderNotConfirmedError(ValueError):
    """Raised when a Shipment cannot be attached to the current Order state."""

    def __init__(self, order_id: UUID, status: OrderStatus) -> None:
        self.order_id = order_id
        self.status = status
        super().__init__(
            f"Order {order_id} must be CONFIRMED to create a Shipment; "
            f"current state is {status.value}."
        )


class OrdersPublic:
    """Public transaction-participating facade consumed by Shipments."""

    def __init__(self, session: AsyncSession) -> None:
        self._repository = OrderRepository(session)

    async def require_confirmed_for_shipment(
        self,
        order_id: UUID,
        *,
        for_update: bool,
    ) -> OrderForShipment:
        """Lock when requested and disclose only Shipment creation data."""
        order = await self._repository.get(order_id, for_update=for_update)
        if order is None:
            raise OrderNotFoundError(order_id)
        if order.status is not OrderStatus.CONFIRMED:
            raise OrderNotConfirmedError(order_id, order.status)
        return _for_shipment(order)

    async def find_id_by_external_reference(self, external_reference: str) -> UUID | None:
        """Resolve an Order list filter without cross-module table access."""
        return await self._repository.find_id_by_external_reference(external_reference)

    async def lock_for_completion(self, order_id: UUID) -> Order:
        """Acquire the Order lock after the caller has locked its Shipment."""
        order = await self._repository.get(order_id, for_update=True)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    async def lock_for_detail(self, order_id: UUID) -> Order:
        """Hold a shared Order lock while a composition layer reads its Shipments."""
        order = await self._repository.get(order_id, for_share=True)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    async def complete_locked(self, order: Order, occurred_at: datetime) -> bool:
        """Idempotently complete an already locked Order in the caller transaction."""
        changed = order.fulfill(occurred_at)
        if changed:
            await self._repository.save(order)
        return changed


def _for_shipment(order: Order) -> OrderForShipment:
    return OrderForShipment(
        id=order.id,
        external_reference=order.external_reference,
        recipient_email=order.recipient_email,
        status=order.status,
    )
