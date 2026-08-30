"""Cross-module read composition with explicit consistency boundaries."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.orders.public import Order, OrdersPublic
from fulfillflow.shipments.public import ShipmentsPublic, ShipmentSummary


@dataclass(frozen=True, slots=True)
class OrderDetail:
    """Consistent Order and Shipment-owned summary projection."""

    order: Order
    shipments: list[ShipmentSummary]


class OrderDetailQuery:
    """Compose an Order detail while preventing incompatible concurrent writes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._orders = OrdersPublic(session)
        self._shipments = ShipmentsPublic(session)

    async def get(self, order_id: UUID) -> OrderDetail:
        """Hold a shared Order lock until its Shipment summaries have been read."""
        async with self._session.begin():
            order = await self._orders.lock_for_detail(order_id)
            shipments = await self._shipments.summaries_for_order(order_id)
        return OrderDetail(order, shipments)
