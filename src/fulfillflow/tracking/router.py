"""Tracking HTTP routes; all service-to-service requests require internal auth."""

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.config import Settings
from fulfillflow.http.body import read_limited_body as _read_limited_body
from fulfillflow.http.dependencies import get_clock, get_session, get_settings
from fulfillflow.http.internal import trace_headers
from fulfillflow.shared import Clock
from fulfillflow.tracking.core_client import CoreClient
from fulfillflow.tracking.public import (
    InboxStatus,
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

router = APIRouter(prefix="/internal/v1/tracking", include_in_schema=False)
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
ClockDependency = Annotated[Clock, Depends(get_clock)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]


def _core_client(request: Request) -> CoreClient:
    return CoreClient(
        cast(httpx.AsyncClient, request.app.state.service_client),
        cast(Settings, request.app.state.settings),
        cast(UUID, request.state.request_id),
        trace_headers(request),
    )


@router.get(
    "/shipments/{shipment_id}/tracking",
    response_model=TrackingEventList,
    tags=["tracking"],
)
async def get_shipment_tracking(
    shipment_id: UUID,
    session: SessionDependency,
    request: Request,
    clock: ClockDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> TrackingEventList:
    """Return the canonical append-only timeline for a Shipment."""
    result = await TrackingService(session, clock, _core_client(request)).timeline(
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
    outcome = await TrackingService(session, clock, _core_client(request)).authenticate_and_process(
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
    request: Request,
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
    result = await TrackingService(session, clock, _core_client(request)).list_inbox(
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
    request: Request,
    clock: ClockDependency,
) -> CarrierEventRead:
    """Return one sanitized inbox detail without exposing authenticated bytes."""
    return CarrierEventRead.from_view(
        await TrackingService(session, clock, _core_client(request)).get_inbox(inbox_event_id)
    )


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
    raise TrackingProblemError(
        status_code=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail=f"Active Carrier '{carrier_code}' was not found.",
    )


def _as_datetime(value: AwareDatetime | None) -> datetime | None:
    return value
