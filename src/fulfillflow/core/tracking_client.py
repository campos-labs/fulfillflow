"""Core forwards public Tracking operations without allocating a SQL session."""

from dataclasses import asdict
from typing import cast
from uuid import UUID

import httpx
from fastapi import Request
from opentelemetry.trace import StatusCode
from starlette.responses import Response

from fulfillflow.config import Settings
from fulfillflow.contracts.problems import RemoteServiceUnavailableError, ServiceProblemError
from fulfillflow.contracts.tracking import (
    CarrierEventFilters,
    CarrierEventList,
    CarrierEventRead,
    TrackingEventList,
)
from fulfillflow.http.body import read_limited_body
from fulfillflow.http.internal import ServiceClient, query_params, trace_headers
from fulfillflow.http.telemetry import PROPAGATOR, operation

_PREFIX = "/internal/v1/tracking"
_FORWARDED_HEADERS = frozenset(
    {
        "content-type",
        "x-fulfillflow-event-id",
        "x-fulfillflow-timestamp",
        "x-fulfillflow-signature",
    }
)


class TrackingClient(ServiceClient):
    async def timeline(self, shipment_id: UUID, *, page: int, page_size: int) -> TrackingEventList:
        return await self.read(
            "GET",
            f"{_PREFIX}/shipments/{shipment_id}/tracking",
            TrackingEventList,
            params={"page": page, "page_size": page_size},
        )

    async def list_inbox(
        self, filters: CarrierEventFilters, *, page: int, page_size: int
    ) -> CarrierEventList:
        return await self.read(
            "GET",
            f"{_PREFIX}/carrier-events",
            CarrierEventList,
            params=query_params(dict(asdict(filters), page=page, page_size=page_size)),
        )

    async def get_inbox(self, inbox_id: UUID) -> CarrierEventRead:
        return await self.read("GET", f"{_PREFIX}/carrier-events/{inbox_id}", CarrierEventRead)


def get_tracking(request: Request) -> TrackingClient:
    return TrackingClient(
        cast(httpx.AsyncClient, request.app.state.service_client),
        cast(Settings, request.app.state.settings),
        cast(UUID, request.state.request_id),
        trace_headers(request),
    )


async def forward_tracking(request: Request) -> Response:
    """Preserve raw bytes, repeated authentication headers, status and public JSON."""
    settings = cast(Settings, request.app.state.settings)
    content = await read_limited_body(request, settings.max_webhook_body_bytes)
    path = _PREFIX + request.url.path.removeprefix("/api/v1")
    if request.url.query:
        path += "?" + request.url.query
    tracer = getattr(request.app.state, "http_tracer", None)
    with operation(tracer, request.method, path.split("?", 1)[0]) as span:
        context: dict[str, str] | None = None
        if span is not None:
            context = {}
            PROPAGATOR.inject(context)
            context = {"traceparent": context["traceparent"]}
        try:
            result = await get_tracking(request).request(
                request.method,
                path,
                content=content,
                headers=[
                    (key, value)
                    for key, value in request.headers.raw
                    if key.decode("latin1") in _FORWARDED_HEADERS
                ],
                trace_context=context,
            )
        except RemoteServiceUnavailableError:
            if span is not None:
                span.set_attribute("error.type", "transport")
                span.set_status(StatusCode.ERROR)
            raise
        if span is not None:
            span.set_attribute("http.response.status_code", result.status_code)
            span.add_event("response_received")
            if result.status_code >= 400:
                span.set_attribute("error.type", "remote_http_error")
                span.set_status(StatusCode.ERROR)
        content_type = result.headers.get("content-type", "")
        if not (
            content_type.startswith("application/json")
            or content_type.startswith("application/problem+json")
        ):
            if span is not None:
                span.set_attribute("error.type", "invalid_content_type")
                span.set_status(StatusCode.ERROR)
            raise ServiceProblemError(
                status_code=503,
                code="SERVICE_UNAVAILABLE",
                title="Service unavailable",
                detail="An internal service could not complete the request.",
            )
        if span is not None:
            span.add_event("content_type_validated")
    return Response(
        content=result.content,
        status_code=result.status_code,
        headers={
            "Content-Type": content_type,
            **{
                key: result.headers[key]
                for key in ("Location", "Retry-After")
                if key in result.headers
            },
        },
    )
