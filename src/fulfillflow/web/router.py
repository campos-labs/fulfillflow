"""Server-rendered operational routes over public application contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from fulfillflow.api.dependencies import get_clock, get_session
from fulfillflow.contracts.tracking import (
    CarrierEventFilters,
)
from fulfillflow.contracts.values import InboxStatus
from fulfillflow.core.tracking_client import get_tracking
from fulfillflow.notifications.public import NotificationService, NotificationStatus
from fulfillflow.notifications.schemas import (
    NotificationFilters,
    NotificationRead,
)
from fulfillflow.orders.public import CreateOrderCommand, OrderService, OrderStatus
from fulfillflow.orders.schemas import OrderFilters, OrderRead
from fulfillflow.shared import Clock
from fulfillflow.shipments.public import (
    CreateShipmentCommand,
    ShipmentService,
    ShipmentStatus,
)
from fulfillflow.shipments.schemas import (
    ShipmentListFilters,
    ShipmentRead,
)
from fulfillflow.web.forms import (
    FormBoundaryError,
    InboxListQuery,
    NotificationListQuery,
    OrderListQuery,
    ShipmentListQuery,
    ShipmentNewQuery,
    aware_datetime,
    parse_query,
    query_values,
    read_strict_form,
    validate_order_form,
    validate_shipment_form,
    validation_messages,
)
from fulfillflow.web.queries import get_dashboard, get_order_detail
from fulfillflow.web.responses import (
    redirect_with_flash,
    render_page,
    render_problem,
)
from fulfillflow.web.security import verify_csrf

router = APIRouter(include_in_schema=False)
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
ClockDependency = Annotated[Clock, Depends(get_clock)]
_PAGE_SIZE = 25


@dataclass(frozen=True, slots=True)
class CarrierChoice:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class Pager:
    page: int
    page_size: int
    total: int
    previous_url: str | None
    next_url: str | None


_CARRIERS = (
    CarrierChoice("carrier-alpha", "Carrier Alpha"),
    CarrierChoice("carrier-beta", "Carrier Beta"),
)

_ORDER_FORM_FIELDS = {
    "external_reference",
    "recipient_name",
    "recipient_email",
    "recipient_postal_code",
    "recipient_city",
    "recipient_state",
}
_SHIPMENT_FORM_FIELDS = {
    "order_id",
    "carrier_code",
    "tracking_code",
    "estimated_delivery_date",
}


@router.get("/", name="web-dashboard")
async def dashboard(
    request: Request,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    view = await get_dashboard(session, clock, get_tracking(request))
    return render_page(
        request,
        "content/dashboard.html",
        title="Dashboard",
        section="dashboard",
        context={"dashboard": view},
    )


@router.get("/orders", name="web-orders-list")
async def list_orders(
    request: Request,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    try:
        raw = query_values(
            request,
            {"status", "external_reference", "created_from", "created_to", "page"},
        )
        query = parse_query(OrderListQuery, raw)
    except (FormBoundaryError, ValidationError) as error:
        return _query_error(request, error)
    result = await OrderService(session, clock).list(
        OrderFilters(
            status=query.status,
            external_reference=query.external_reference,
            created_from=aware_datetime(query.created_from),
            created_to=aware_datetime(query.created_to),
        ),
        page=query.page,
        page_size=_PAGE_SIZE,
    )
    return render_page(
        request,
        "content/orders_list.html",
        title="Orders",
        section="orders",
        context={
            "orders": [OrderRead.from_order(item) for item in result.items],
            "order_statuses": tuple(OrderStatus),
            "filters": _filter_values(
                request,
                ("status", "external_reference", "created_from", "created_to"),
            ),
            "pager": _pager(request, result.page, result.page_size, result.total),
        },
    )


# The static /new route intentionally precedes /orders/{order_id}.
@router.get("/orders/new", name="web-orders-new")
async def new_order(request: Request) -> Response:
    return _order_form_response(request, values={}, errors={})


@router.post("/orders", name="web-orders-create")
async def create_order(
    request: Request,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    try:
        submitted = await read_strict_form(request, fields=_ORDER_FORM_FIELDS)
    except FormBoundaryError as error:
        return _order_form_response(
            request,
            values={},
            errors={"form": [error.message]},
            status_code=422,
        )
    if not verify_csrf(request, submitted.csrf_token):
        return _csrf_error(request)
    try:
        payload = validate_order_form(submitted.fields)
    except ValidationError as error:
        return _order_form_response(
            request,
            values=submitted.fields,
            errors=validation_messages(error),
            status_code=422,
        )
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
    return redirect_with_flash(
        request,
        "web-order-detail",
        flash="order-created",
        path_params={"order_id": order.id},
    )


@router.get("/orders/{order_id}", name="web-order-detail")
async def order_detail(
    request: Request,
    order_id: UUID,
    session: SessionDependency,
) -> Response:
    detail = await get_order_detail(session, order_id)
    return render_page(
        request,
        "content/order_detail.html",
        title=f"Order {detail.order.external_reference}",
        section="orders",
        context={"detail": detail},
    )


@router.post("/orders/{order_id}/confirm", name="web-order-confirm")
async def confirm_order(
    request: Request,
    order_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    csrf_error = await _verify_action_form(request)
    if csrf_error is not None:
        return csrf_error
    await OrderService(session, clock).confirm(order_id)
    return redirect_with_flash(
        request,
        "web-order-detail",
        flash="order-confirmed",
        path_params={"order_id": order_id},
    )


@router.post("/orders/{order_id}/cancel", name="web-order-cancel")
async def cancel_order(
    request: Request,
    order_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    csrf_error = await _verify_action_form(request)
    if csrf_error is not None:
        return csrf_error
    await OrderService(session, clock).cancel(order_id)
    return redirect_with_flash(
        request,
        "web-order-detail",
        flash="order-cancelled",
        path_params={"order_id": order_id},
    )


@router.get("/shipments", name="web-shipments-list")
async def list_shipments(
    request: Request,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    try:
        raw = query_values(
            request,
            {
                "status",
                "carrier_code",
                "order_external_reference",
                "tracking_code",
                "created_from",
                "created_to",
                "page",
            },
        )
        query = parse_query(ShipmentListQuery, raw)
    except (FormBoundaryError, ValidationError) as error:
        return _query_error(request, error)
    result = await ShipmentService(session, clock).list(
        ShipmentListFilters(
            status=query.status,
            carrier_code=query.carrier_code,
            order_external_reference=query.order_external_reference,
            tracking_code=query.tracking_code,
            created_from=aware_datetime(query.created_from),
            created_to=aware_datetime(query.created_to),
        ),
        page=query.page,
        page_size=_PAGE_SIZE,
    )
    return render_page(
        request,
        "content/shipments_list.html",
        title="Shipments",
        section="shipments",
        context={
            "shipments": [ShipmentRead.from_view(item) for item in result.items],
            "shipment_statuses": tuple(ShipmentStatus),
            "carriers": _CARRIERS,
            "filters": _filter_values(
                request,
                (
                    "status",
                    "carrier_code",
                    "order_external_reference",
                    "tracking_code",
                    "created_from",
                    "created_to",
                ),
            ),
            "pager": _pager(request, result.page, result.page_size, result.total),
        },
    )


# The static /new route intentionally precedes /shipments/{shipment_id}.
@router.get("/shipments/new", name="web-shipments-new")
async def new_shipment(request: Request) -> Response:
    try:
        raw = query_values(request, {"order_id"})
        query = parse_query(ShipmentNewQuery, raw)
    except (FormBoundaryError, ValidationError) as error:
        return _query_error(request, error)
    values = {"order_id": str(query.order_id) if query.order_id is not None else ""}
    return _shipment_form_response(request, values=values, errors={})


@router.post("/shipments", name="web-shipments-create")
async def create_shipment(
    request: Request,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    try:
        submitted = await read_strict_form(request, fields=_SHIPMENT_FORM_FIELDS)
    except FormBoundaryError as error:
        return _shipment_form_response(
            request,
            values={},
            errors={"form": [error.message]},
            status_code=422,
        )
    if not verify_csrf(request, submitted.csrf_token):
        return _csrf_error(request)
    try:
        payload = validate_shipment_form(submitted.fields)
    except ValidationError as error:
        return _shipment_form_response(
            request,
            values=submitted.fields,
            errors=validation_messages(error),
            status_code=422,
        )
    shipment = await ShipmentService(session, clock).create(
        CreateShipmentCommand(
            order_id=payload.order_id,
            carrier_code=payload.carrier_code,
            tracking_code=payload.tracking_code,
            estimated_delivery_date=payload.estimated_delivery_date,
        )
    )
    return redirect_with_flash(
        request,
        "web-shipment-detail",
        flash="shipment-created",
        path_params={"shipment_id": shipment.shipment.id},
    )


@router.get("/shipments/{shipment_id}", name="web-shipment-detail")
async def shipment_detail(
    request: Request,
    shipment_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    shipment = ShipmentRead.from_view(await ShipmentService(session, clock).get(shipment_id))
    return render_page(
        request,
        "content/shipment_detail.html",
        title=f"Shipment {shipment.tracking_code}",
        section="shipments",
        context={"shipment": shipment},
    )


@router.post("/shipments/{shipment_id}/cancel", name="web-shipment-cancel")
async def cancel_shipment(
    request: Request,
    shipment_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    csrf_error = await _verify_action_form(request)
    if csrf_error is not None:
        return csrf_error
    await ShipmentService(session, clock).cancel(shipment_id)
    return redirect_with_flash(
        request,
        "web-shipment-detail",
        flash="shipment-cancelled",
        path_params={"shipment_id": shipment_id},
    )


@router.get("/shipments/{shipment_id}/tracking", name="web-shipment-tracking")
async def shipment_tracking(
    request: Request,
    shipment_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    try:
        raw = query_values(request, {"page"})
        page = int(raw.get("page", "1"))
        if page < 1:
            raise ValueError
    except (FormBoundaryError, ValueError) as error:
        return _query_error(request, error)
    shipment = ShipmentRead.from_view(await ShipmentService(session, clock).get(shipment_id))
    result = await get_tracking(request).timeline(
        shipment_id,
        page=page,
        page_size=_PAGE_SIZE,
    )
    return render_page(
        request,
        "content/tracking.html",
        title=f"Tracking {shipment.tracking_code}",
        section="shipments",
        context={
            "shipment": shipment,
            "events": result.items,
            "pager": _pager(request, result.page, result.page_size, result.total),
        },
    )


@router.get("/carrier-events", name="web-inbox-list")
async def list_inbox(
    request: Request,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    try:
        raw = query_values(
            request,
            {
                "carrier_code",
                "status",
                "external_event_id",
                "received_from",
                "received_to",
                "page",
            },
        )
        query = parse_query(InboxListQuery, raw)
    except (FormBoundaryError, ValidationError) as error:
        return _query_error(request, error)
    result = await get_tracking(request).list_inbox(
        CarrierEventFilters(
            carrier_code=query.carrier_code,
            status=query.status,
            external_event_id=query.external_event_id,
            received_from=aware_datetime(query.received_from),
            received_to=aware_datetime(query.received_to),
        ),
        page=query.page,
        page_size=_PAGE_SIZE,
    )
    return render_page(
        request,
        "content/inbox_list.html",
        title="Carrier event inbox",
        section="inbox",
        context={
            "events": result.items,
            "carriers": _CARRIERS,
            "inbox_statuses": tuple(InboxStatus),
            "filters": _filter_values(
                request,
                (
                    "carrier_code",
                    "status",
                    "external_event_id",
                    "received_from",
                    "received_to",
                ),
            ),
            "pager": _pager(request, result.page, result.page_size, result.total),
        },
    )


@router.get("/carrier-events/{inbox_event_id}", name="web-inbox-detail")
async def inbox_detail(
    request: Request,
    inbox_event_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> Response:
    event = await get_tracking(request).get_inbox(inbox_event_id)
    return render_page(
        request,
        "content/inbox_detail.html",
        title=f"Carrier event {event.external_event_id}",
        section="inbox",
        context={"event": event},
    )


@router.get("/notifications", name="web-notifications-list")
async def list_notifications(
    request: Request,
    session: SessionDependency,
) -> Response:
    try:
        raw = query_values(
            request,
            {"status", "shipment_id", "created_from", "created_to", "page"},
        )
        query = parse_query(NotificationListQuery, raw)
    except (FormBoundaryError, ValidationError) as error:
        return _query_error(request, error)
    result = await NotificationService(session).list(
        NotificationFilters(
            status=query.status,
            shipment_id=query.shipment_id,
            created_from=aware_datetime(query.created_from),
            created_to=aware_datetime(query.created_to),
        ),
        page=query.page,
        page_size=_PAGE_SIZE,
    )
    return render_page(
        request,
        "content/notifications_list.html",
        title="Notifications",
        section="notifications",
        context={
            "notifications": [NotificationRead.from_notification(item) for item in result.items],
            "notification_statuses": tuple(NotificationStatus),
            "filters": _filter_values(
                request,
                ("status", "shipment_id", "created_from", "created_to"),
            ),
            "pager": _pager(request, result.page, result.page_size, result.total),
        },
    )


@router.get("/notifications/{notification_id}", name="web-notification-detail")
async def notification_detail(
    request: Request,
    notification_id: UUID,
    session: SessionDependency,
) -> Response:
    notification = NotificationRead.from_notification(
        await NotificationService(session).get(notification_id)
    )
    return render_page(
        request,
        "content/notification_detail.html",
        title="Notification simulation",
        section="notifications",
        context={"notification": notification},
    )


@router.get("/simulator", name="web-simulator")
async def simulator_instructions(request: Request) -> Response:
    return render_page(
        request,
        "content/simulator.html",
        title="External Carrier simulator",
        section="simulator",
    )


def _order_form_response(
    request: Request,
    *,
    values: dict[str, str],
    errors: dict[str, list[str]],
    status_code: int = 200,
) -> Response:
    complete_values = {name: values.get(name, "") for name in _ORDER_FORM_FIELDS}
    return render_page(
        request,
        "content/order_form.html",
        title="Create Order",
        section="orders",
        status_code=status_code,
        context={
            "values": complete_values,
            "field_errors_map": errors,
            "request_id": request.state.request_id,
        },
    )


def _shipment_form_response(
    request: Request,
    *,
    values: dict[str, str],
    errors: dict[str, list[str]],
    status_code: int = 200,
) -> Response:
    complete_values = {name: values.get(name, "") for name in _SHIPMENT_FORM_FIELDS}
    return render_page(
        request,
        "content/shipment_form.html",
        title="Create Shipment",
        section="shipments",
        status_code=status_code,
        context={
            "values": complete_values,
            "field_errors_map": errors,
            "request_id": request.state.request_id,
            "carriers": _CARRIERS,
        },
    )


async def _verify_action_form(request: Request) -> Response | None:
    try:
        submitted = await read_strict_form(request, fields=())
    except FormBoundaryError as error:
        return render_problem(
            request,
            status_code=422,
            code="VALIDATION_ERROR",
            title="Form validation failed",
            detail=error.message,
        )
    return None if verify_csrf(request, submitted.csrf_token) else _csrf_error(request)


def _csrf_error(request: Request) -> Response:
    return render_problem(
        request,
        status_code=403,
        code="CSRF_VALIDATION_FAILED",
        title="CSRF validation failed",
        detail="The form token is missing, expired, or invalid.",
    )


def _query_error(request: Request, error: Exception) -> Response:
    if isinstance(error, ValidationError):
        details = [
            {
                "location": [str(part) for part in item["loc"]],
                "message": str(item["msg"]),
                "error_type": str(item["type"]),
            }
            for item in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        ]
    else:
        details = [
            {"location": ["query"], "message": "Invalid query.", "error_type": "value_error"}
        ]
    return render_problem(
        request,
        status_code=422,
        code="VALIDATION_ERROR",
        title="Query validation failed",
        detail="The query contains invalid fields or parameters.",
        errors=details,
    )


def _filter_values(request: Request, names: tuple[str, ...]) -> dict[str, str]:
    return {name: request.query_params.get(name, "") for name in names}


def _pager(request: Request, page: int, page_size: int, total: int) -> Pager:
    previous_url = _page_url(request, page - 1) if page > 1 else None
    next_url = _page_url(request, page + 1) if page * page_size < total else None
    return Pager(page, page_size, total, previous_url, next_url)


def _page_url(request: Request, page: int) -> str:
    values = dict(request.query_params)
    values["page"] = str(page)
    return f"{request.url.path}?{urlencode(values)}"
