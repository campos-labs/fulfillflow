"""REST routes composed exclusively over public module services and schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from fulfillflow.api.dependencies import get_clock, get_session, get_settings
from fulfillflow.api.queries import OrderDetailQuery
from fulfillflow.api.schemas import OrderDetailRead
from fulfillflow.config import Settings
from fulfillflow.contracts.tracking import (
    CarrierEventList,
    CarrierEventRead,
    TrackingEventList,
    WebhookResponse,
)
from fulfillflow.contracts.values import InboxStatus
from fulfillflow.core.tracking_client import forward_tracking
from fulfillflow.notifications.public import NotificationService, NotificationStatus
from fulfillflow.notifications.schemas import (
    NotificationFilters,
    NotificationList,
    NotificationRead,
)
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
SettingsDependency = Annotated[Settings, Depends(get_settings)]


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


@router.get(
    "/shipments/{shipment_id}/tracking", response_model=TrackingEventList, tags=["tracking"]
)
async def get_shipment_tracking(
    shipment_id: UUID,
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Response:
    return await forward_tracking(request)


@router.post("/carriers/{carrier_code}/events", response_model=WebhookResponse, tags=["tracking"])
async def receive_carrier_event(carrier_code: str, request: Request) -> Response:
    return await forward_tracking(request)


@router.get("/carrier-events", response_model=CarrierEventList, tags=["tracking"])
async def list_carrier_events(
    request: Request,
    carrier_code: str | None = None,
    inbox_status: Annotated[InboxStatus | None, Query(alias="status")] = None,
    external_event_id: Annotated[str | None, Query(max_length=128)] = None,
    received_from: AwareDatetime | None = None,
    received_to: AwareDatetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Response:
    return await forward_tracking(request)


@router.get("/carrier-events/{inbox_event_id}", response_model=CarrierEventRead, tags=["tracking"])
async def get_carrier_event(inbox_event_id: UUID, request: Request) -> Response:
    return await forward_tracking(request)


@router.get(
    "/notifications",
    response_model=NotificationList,
    tags=["notifications"],
)
async def list_notifications(
    session: SessionDependency,
    notification_status: Annotated[NotificationStatus | None, Query(alias="status")] = None,
    shipment_id: UUID | None = None,
    created_from: AwareDatetime | None = None,
    created_to: AwareDatetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> NotificationList:
    """List simulated Notification records through their operational projection."""
    result = await NotificationService(session).list(
        NotificationFilters(
            status=notification_status,
            shipment_id=shipment_id,
            created_from=_as_datetime(created_from),
            created_to=_as_datetime(created_to),
        ),
        page=page,
        page_size=page_size,
    )
    return NotificationList(
        items=[NotificationRead.from_notification(item) for item in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.get(
    "/notifications/{notification_id}",
    response_model=NotificationRead,
    tags=["notifications"],
)
async def get_notification(
    notification_id: UUID,
    session: SessionDependency,
) -> NotificationRead:
    """Return one simulated Notification without exposing causal payload internals."""
    return NotificationRead.from_notification(
        await NotificationService(session).get(notification_id)
    )


def _as_datetime(value: AwareDatetime | None) -> datetime | None:
    return value
