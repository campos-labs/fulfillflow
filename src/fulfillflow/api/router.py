"""REST routes composed exclusively over public module services and schemas."""

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.api.dependencies import get_clock, get_session, get_settings
from fulfillflow.api.queries import OrderDetailQuery
from fulfillflow.api.schemas import OrderDetailRead
from fulfillflow.carriers.public import CarrierNotFoundError
from fulfillflow.config import Settings
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
from fulfillflow.tracking.public import (
    InboxStatus,
    PayloadTooLargeError,
    TrackingProblemError,
    TrackingService,
    UnsupportedWebhookMediaTypeError,
    WebhookAuthentication,
)
from fulfillflow.tracking.schemas import (
    CarrierEventFilters,
    CarrierEventList,
    CarrierEventRead,
    CarrierEventSummaryRead,
    TrackingEventList,
    TrackingEventRead,
    WebhookResponse,
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
    "/shipments/{shipment_id}/tracking",
    response_model=TrackingEventList,
    tags=["tracking"],
)
async def get_shipment_tracking(
    shipment_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> TrackingEventList:
    """Return the canonical append-only timeline for a Shipment."""
    result = await TrackingService(session, clock).timeline(
        shipment_id,
        page=page,
        page_size=page_size,
    )
    return TrackingEventList(
        items=[TrackingEventRead.from_event(event) for event in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.post(
    "/carriers/{carrier_code}/events",
    response_model=WebhookResponse,
    tags=["tracking"],
)
async def receive_carrier_event(
    carrier_code: str,
    request: Request,
    session: SessionDependency,
    clock: ClockDependency,
    settings: SettingsDependency,
) -> WebhookResponse:
    """Authenticate and synchronously process one raw Carrier webhook."""
    normalized_code = carrier_code.strip().lower()
    secret = _carrier_secret(settings, normalized_code)
    authentication = WebhookAuthentication(
        event_id=_single_webhook_header(request, "X-FulfillFlow-Event-Id"),
        timestamp=_single_webhook_header(request, "X-FulfillFlow-Timestamp"),
        signature=_single_webhook_header(request, "X-FulfillFlow-Signature"),
    )
    content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
    if content_type.strip().lower() != "application/json":
        raise UnsupportedWebhookMediaTypeError
    raw_body = await _read_limited_body(request, settings.max_webhook_body_bytes)
    outcome = await TrackingService(session, clock).authenticate_and_process(
        normalized_code,
        authentication,
        raw_body,
        secret=secret,
        tolerance_seconds=settings.webhook_signature_tolerance_seconds,
        request_id=cast(UUID, request.state.request_id),
    )
    return WebhookResponse.from_outcome(outcome)


@router.get(
    "/carrier-events",
    response_model=CarrierEventList,
    tags=["tracking"],
)
async def list_carrier_events(
    session: SessionDependency,
    clock: ClockDependency,
    carrier_code: str | None = None,
    inbox_status: Annotated[InboxStatus | None, Query(alias="status")] = None,
    external_event_id: Annotated[str | None, Query(max_length=128)] = None,
    received_from: AwareDatetime | None = None,
    received_to: AwareDatetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> CarrierEventList:
    """List authenticated inbox records through sanitized projections."""
    result = await TrackingService(session, clock).list_inbox(
        CarrierEventFilters(
            carrier_code=carrier_code,
            status=inbox_status,
            external_event_id=external_event_id,
            received_from=_as_datetime(received_from),
            received_to=_as_datetime(received_to),
        ),
        page=page,
        page_size=page_size,
    )
    return CarrierEventList(
        items=[CarrierEventSummaryRead.from_view(view) for view in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.get(
    "/carrier-events/{inbox_event_id}",
    response_model=CarrierEventRead,
    tags=["tracking"],
)
async def get_carrier_event(
    inbox_event_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> CarrierEventRead:
    """Return one sanitized inbox detail without exposing authenticated bytes."""
    return CarrierEventRead.from_view(
        await TrackingService(session, clock).get_inbox(inbox_event_id)
    )


async def _read_limited_body(request: Request, maximum_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum_bytes:
                raise PayloadTooLargeError
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise PayloadTooLargeError
        body.extend(chunk)
    return bytes(body)


def _single_webhook_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise TrackingProblemError(
            status_code=401,
            code="INVALID_WEBHOOK_SIGNATURE",
            title="Invalid webhook signature",
            detail="Webhook authentication failed.",
        )
    return values[0]


def _carrier_secret(settings: Settings, carrier_code: str) -> str:
    if carrier_code == "carrier-alpha":
        return settings.carrier_alpha_webhook_secret.get_secret_value()
    if carrier_code == "carrier-beta":
        return settings.carrier_beta_webhook_secret.get_secret_value()
    raise CarrierNotFoundError(carrier_code)


def _as_datetime(value: AwareDatetime | None) -> datetime | None:
    return value
