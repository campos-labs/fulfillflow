"""Orders-owned persistence operations; this module never commits."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.orders.domain import Order, OrderStatus
from fulfillflow.orders.models import OrderModel
from fulfillflow.shared.pagination import Page


class OrderRepository:
    """Map Order entities to their module-owned SQLAlchemy records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, order: Order) -> None:
        """Stage and flush a new Order without committing its transaction."""
        self._session.add(_to_model(order))
        await self._session.flush()

    async def get(
        self,
        order_id: UUID,
        *,
        for_update: bool = False,
        for_share: bool = False,
    ) -> Order | None:
        """Load one Order, optionally with an exclusive or shared row lock."""
        if for_update and for_share:
            raise ValueError("Order lock mode must be either update or share")
        statement = select(OrderModel).where(OrderModel.id == order_id)
        if for_update:
            statement = statement.with_for_update()
        elif for_share:
            statement = statement.with_for_update(read=True)
        model = await self._session.scalar(statement)
        return _to_entity(model) if model is not None else None

    async def find_id_by_external_reference(self, external_reference: str) -> UUID | None:
        """Resolve a public reference without exposing the ORM model."""
        result = await self._session.execute(
            select(OrderModel.id).where(OrderModel.external_reference == external_reference)
        )
        return result.scalar_one_or_none()

    async def save(self, order: Order) -> None:
        """Persist mutable entity fields and flush without committing."""
        model = await self._session.get(OrderModel, order.id)
        if model is None:
            raise LookupError(f"Order {order.id} disappeared during its transaction")
        model.status = order.status.value
        model.updated_at = order.updated_at
        await self._session.flush()

    async def list(
        self,
        *,
        status: OrderStatus | None,
        external_reference: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
    ) -> Page[Order]:
        """Return a stable newest-first page and matching total."""
        predicates = []
        if status is not None:
            predicates.append(OrderModel.status == status.value)
        if external_reference is not None:
            predicates.append(OrderModel.external_reference == external_reference)
        if created_from is not None:
            predicates.append(OrderModel.created_at >= created_from)
        if created_to is not None:
            predicates.append(OrderModel.created_at <= created_to)

        total = await self._session.scalar(
            select(func.count()).select_from(OrderModel).where(*predicates)
        )
        statement = (
            select(OrderModel)
            .where(*predicates)
            .order_by(OrderModel.created_at.desc(), OrderModel.id.desc())
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


def _to_model(order: Order) -> OrderModel:
    return OrderModel(
        id=order.id,
        external_reference=order.external_reference,
        recipient_name=order.recipient_name,
        recipient_email=order.recipient_email,
        recipient_postal_code=order.recipient_postal_code,
        recipient_city=order.recipient_city,
        recipient_state=order.recipient_state,
        status=order.status.value,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


def _to_entity(model: OrderModel) -> Order:
    return Order(
        id=model.id,
        external_reference=model.external_reference,
        recipient_name=model.recipient_name,
        recipient_email=model.recipient_email,
        recipient_postal_code=model.recipient_postal_code,
        recipient_city=model.recipient_city,
        recipient_state=model.recipient_state,
        status=OrderStatus(model.status),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )
