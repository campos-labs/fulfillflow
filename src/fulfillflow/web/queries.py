"""Read-only web compositions over public module contracts."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.notifications.public import NotificationService, NotificationStatus
from fulfillflow.notifications.schemas import NotificationFilters
from fulfillflow.orders.public import OrderService, OrdersPublic, OrderStatus
from fulfillflow.orders.schemas import OrderFilters, OrderRead
from fulfillflow.shared import Clock
from fulfillflow.shipments.public import (
    ShipmentService,
    ShipmentsPublic,
    ShipmentStatus,
)
from fulfillflow.shipments.schemas import (
    ShipmentListFilters,
    ShipmentSummaryRead,
)
from fulfillflow.tracking.public import InboxStatus, TrackingService
from fulfillflow.tracking.schemas import CarrierEventFilters, CarrierEventSummaryRead

_COUNT_PAGE_SIZE = 1
_RECENT_EVENT_COUNT = 10


@dataclass(frozen=True, slots=True)
class OrderDetailView:
    """Consistent Order detail composed only from public facades and schemas."""

    order: OrderRead
    shipments: list[ShipmentSummaryRead]


@dataclass(frozen=True, slots=True)
class DashboardView:
    """Operational counts plus sanitized recent inbox projections."""

    order_counts: dict[str, int]
    shipment_counts: dict[str, int]
    inbox_counts: dict[str, int]
    notification_counts: dict[str, int]
    recent_events: list[CarrierEventSummaryRead]


async def get_order_detail(session: AsyncSession, order_id: UUID) -> OrderDetailView:
    """Hold a shared Order lock while reading Shipment-owned summaries."""
    async with session.begin():
        order = await OrdersPublic(session).lock_for_detail(order_id)
        shipments = await ShipmentsPublic(session).summaries_for_order(order_id)
    return OrderDetailView(
        order=OrderRead.from_order(order),
        shipments=[ShipmentSummaryRead.from_summary(item) for item in shipments],
    )


async def get_dashboard(
    session: AsyncSession,
    clock: Clock,
) -> DashboardView:
    """Build the approved dashboard without direct model or repository access."""
    orders = OrderService(session, clock)
    shipments = ShipmentService(session, clock)
    tracking = TrackingService(session, clock)
    notifications = NotificationService(session)

    order_counts: dict[str, int] = {}
    for order_status in OrderStatus:
        order_page = await orders.list(
            OrderFilters(status=order_status),
            page=1,
            page_size=_COUNT_PAGE_SIZE,
        )
        order_counts[order_status.value] = order_page.total

    shipment_counts: dict[str, int] = {}
    for shipment_status in ShipmentStatus:
        shipment_page = await shipments.list(
            ShipmentListFilters(status=shipment_status),
            page=1,
            page_size=_COUNT_PAGE_SIZE,
        )
        shipment_counts[shipment_status.value] = shipment_page.total

    inbox_counts: dict[str, int] = {}
    for inbox_status in InboxStatus:
        inbox_page = await tracking.list_inbox(
            CarrierEventFilters(status=inbox_status),
            page=1,
            page_size=_COUNT_PAGE_SIZE,
        )
        inbox_counts[inbox_status.value] = inbox_page.total

    notification_counts: dict[str, int] = {}
    for notification_status in NotificationStatus:
        notification_page = await notifications.list(
            NotificationFilters(status=notification_status),
            page=1,
            page_size=_COUNT_PAGE_SIZE,
        )
        notification_counts[notification_status.value] = notification_page.total

    recent = await tracking.list_inbox(
        CarrierEventFilters(),
        page=1,
        page_size=_RECENT_EVENT_COUNT,
    )
    return DashboardView(
        order_counts=order_counts,
        shipment_counts=shipment_counts,
        inbox_counts=inbox_counts,
        notification_counts=notification_counts,
        recent_events=[CarrierEventSummaryRead.from_view(item) for item in recent.items],
    )
