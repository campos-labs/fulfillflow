"""REST routes composed exclusively over public module services and schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.api.dependencies import get_clock, get_session
from fulfillflow.api.queries import OrderDetailQuery
from fulfillflow.api.schemas import OrderDetailRead
from fulfillflow.orders.public import (
    CreateOrderCommand,
    OrderService,
    OrderStatus,
)
from fulfillflow.orders.schemas import OrderCreate, OrderFilters, OrderList, OrderRead
from fulfillflow.shared import Clock
from fulfillflow.shipments.public import (
    CreateShipmentCommand,
    ShipmentService,
    ShipmentStatus,
)
from fulfillflow.shipments.schemas import (
    ShipmentCreate,
    ShipmentList,
    ShipmentListFilters,
    ShipmentRead,
    ShipmentSummaryRead,
)

router = APIRouter(prefix="/api/v1")
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
ClockDependency = Annotated[Clock, Depends(get_clock)]


@router.post(
    "/orders",
    response_model=OrderRead,
    status_code=status.HTTP_201_CREATED,
    tags=["orders"],
)
async def create_order(
    payload: OrderCreate,
    session: SessionDependency,
    clock: ClockDependency,
) -> OrderRead:
    """Create an Order in CREATED state."""
    order = await OrderService(session, clock).create(
        CreateOrderCommand(
            external_reference=payload.external_reference,
            recipient_name=payload.recipient.name,
            recipient_email=payload.recipient.email,
            recipient_postal_code=payload.recipient.postal_code,
            recipient_city=payload.recipient.city,
            recipient_state=payload.recipient.state,
        )
    )
    return OrderRead.from_order(order)


@router.get("/orders", response_model=OrderList, tags=["orders"])
async def list_orders(
    session: SessionDependency,
    clock: ClockDependency,
    order_status: Annotated[OrderStatus | None, Query(alias="status")] = None,
    external_reference: str | None = None,
    created_from: AwareDatetime | None = None,
    created_to: AwareDatetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> OrderList:
    """List Orders with stable pagination."""
    result = await OrderService(session, clock).list(
        OrderFilters(
            status=order_status,
            external_reference=external_reference,
            created_from=_as_datetime(created_from),
            created_to=_as_datetime(created_to),
        ),
        page=page,
        page_size=page_size,
    )
    return OrderList(
        items=[OrderRead.from_order(order) for order in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.get("/orders/{order_id}", response_model=OrderDetailRead, tags=["orders"])
async def get_order(
    order_id: UUID,
    session: SessionDependency,
) -> OrderDetailRead:
    """Return Order detail composed with Shipment-owned summaries."""
    detail = await OrderDetailQuery(session).get(order_id)
    order_read = OrderRead.from_order(detail.order)
    return OrderDetailRead(
        **order_read.model_dump(),
        shipments=[ShipmentSummaryRead.from_summary(item) for item in detail.shipments],
    )


@router.post("/orders/{order_id}/confirm", response_model=OrderRead, tags=["orders"])
async def confirm_order(
    order_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> OrderRead:
    """Idempotently confirm a CREATED Order."""
    return OrderRead.from_order(await OrderService(session, clock).confirm(order_id))


@router.post("/orders/{order_id}/cancel", response_model=OrderRead, tags=["orders"])
async def cancel_order(
    order_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> OrderRead:
    """Idempotently cancel a CREATED Order."""
    return OrderRead.from_order(await OrderService(session, clock).cancel(order_id))


@router.post(
    "/shipments",
    response_model=ShipmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["shipments"],
)
async def create_shipment(
    payload: ShipmentCreate,
    session: SessionDependency,
    clock: ClockDependency,
) -> ShipmentRead:
    """Create a PENDING Shipment for one confirmed Order and active Carrier."""
    view = await ShipmentService(session, clock).create(
        CreateShipmentCommand(
            order_id=payload.order_id,
            carrier_code=payload.carrier_code,
            tracking_code=payload.tracking_code,
            estimated_delivery_date=payload.estimated_delivery_date,
        )
    )
    return ShipmentRead.from_view(view)


@router.get("/shipments", response_model=ShipmentList, tags=["shipments"])
async def list_shipments(
    session: SessionDependency,
    clock: ClockDependency,
    shipment_status: Annotated[ShipmentStatus | None, Query(alias="status")] = None,
    carrier_code: str | None = None,
    order_external_reference: str | None = None,
    tracking_code: str | None = None,
    created_from: AwareDatetime | None = None,
    created_to: AwareDatetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ShipmentList:
    """List Shipments using all documented minimum filters."""
    result = await ShipmentService(session, clock).list(
        ShipmentListFilters(
            status=shipment_status,
            carrier_code=carrier_code,
            order_external_reference=order_external_reference,
            tracking_code=tracking_code,
            created_from=_as_datetime(created_from),
            created_to=_as_datetime(created_to),
        ),
        page=page,
        page_size=page_size,
    )
    return ShipmentList(
        items=[ShipmentRead.from_view(view) for view in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.get(
    "/shipments/{shipment_id}",
    response_model=ShipmentRead,
    tags=["shipments"],
)
async def get_shipment(
    shipment_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> ShipmentRead:
    """Return one operational Shipment detail."""
    return ShipmentRead.from_view(await ShipmentService(session, clock).get(shipment_id))


@router.post(
    "/shipments/{shipment_id}/cancel",
    response_model=ShipmentRead,
    tags=["shipments"],
)
async def cancel_shipment(
    shipment_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> ShipmentRead:
    """Idempotently cancel a PENDING Shipment."""
    return ShipmentRead.from_view(await ShipmentService(session, clock).cancel(shipment_id))


def _as_datetime(value: AwareDatetime | None) -> datetime | None:
    return value
