"""Real SDK spans over ASGI/HTTPX; no SQL substitute or external service."""

import json
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import BaseModel

from fulfillflow.config import Settings
from fulfillflow.contracts.problems import RemoteServiceUnavailableError, ServiceProblemError
from fulfillflow.http.internal import ServiceClient
from fulfillflow.http.telemetry import (
    INTERNAL_ROUTE,
    PROPAGATOR,
    PUBLIC_ROUTE,
    DiagnosticMiddleware,
    create_provider,
)


class Result(BaseModel):
    total: int


@pytest.fixture
def telemetry():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({"service.name": "test"}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("test"), exporter
    provider.shutdown()


async def test_chain_is_parented_and_does_not_capture_secrets(settings: Settings, telemetry):
    tracer, exporter = telemetry
    peer = FastAPI()
    peer.state.http_tracer = tracer
    peer.add_middleware(DiagnosticMiddleware)

    @peer.get(INTERNAL_ROUTE)
    async def result():
        return {"total": 1}

    core = FastAPI()
    core.state.http_tracer = tracer
    core.add_middleware(DiagnosticMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=peer), base_url="http://peer"
    ) as inner:

        @core.get(PUBLIC_ROUTE)
        async def get_result():
            service = ServiceClient(inner, settings, uuid4(), tracer=tracer)
            return await service.read(
                "GET", INTERNAL_ROUTE, Result, params={"secret": "NEVER_EXPORT"}
            )

        with tracer.start_as_current_span("observer", kind=SpanKind.CLIENT) as root:
            headers = {"baggage": "private=NEVER_EXPORT", "tracestate": "vendor=NEVER_EXPORT"}
            PROPAGATOR.inject(headers)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=core), base_url="http://core"
            ) as client:
                response = await client.get(PUBLIC_ROUTE + "?secret=NEVER_EXPORT", headers=headers)
    assert response.json() == {"total": 1}
    spans = exporter.get_finished_spans()
    assert len(spans) == 4
    by_id = {span.context.span_id: span for span in spans}
    assert {s.context.trace_id for s in spans} == {root.get_span_context().trace_id}
    child = next(s for s in spans if s.kind == SpanKind.SERVER and s.name.endswith(INTERNAL_ROUTE))
    hop = by_id[child.parent.span_id]
    server = by_id[hop.parent.span_id]
    assert hop.kind == SpanKind.CLIENT
    assert server.name == "GET " + PUBLIC_ROUTE
    assert server.parent.span_id == root.get_span_context().span_id
    assert [e.name for e in hop.events] == ["response_received", "response_validated"]
    assert "NEVER_EXPORT" not in json.dumps([s.to_json() for s in spans])
    assert all(not s.context.trace_state for s in spans)


@pytest.mark.parametrize(
    "mode,category,status",
    [
        ("transport", "transport", None),
        ("invalid200", "invalid_response", 200),
        ("invalid503", "invalid_response", 503),
        ("remote", "remote_problem", 503),
    ],
)
async def test_failure_categories_preserve_contract_without_exception_text(
    settings, telemetry, mode, category, status
):
    tracer, exporter = telemetry

    def respond(request):
        if mode == "transport":
            raise httpx.ConnectError("NEVER_EXPORT", request=request)
        if mode == "remote":
            return httpx.Response(
                503,
                json={
                    "type": "about:blank",
                    "title": "Unavailable",
                    "status": 503,
                    "detail": "NEVER_EXPORT",
                    "code": "SERVICE_UNAVAILABLE",
                    "request_id": str(uuid4()),
                    "errors": [],
                },
            )
        return httpx.Response(status, content=b"NEVER_EXPORT")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://peer"
    ) as client:
        service = ServiceClient(client, settings, uuid4(), tracer=tracer)
        with pytest.raises((RemoteServiceUnavailableError, ServiceProblemError)):
            await service.read("GET", INTERNAL_ROUTE, Result)
    (span,) = exporter.get_finished_spans()
    assert span.attributes["error.type"] == category
    assert span.attributes.get("http.response.status_code") == status
    assert span.status.status_code == StatusCode.ERROR
    assert "NEVER_EXPORT" not in span.to_json()
    assert not any(e.name == "exception" for e in span.events)


@pytest.mark.parametrize(
    "method,path",
    [("POST", INTERNAL_ROUTE), ("GET", "/health/ready"), ("GET", INTERNAL_ROUTE + "/id")],
)
async def test_unselected_operations_preserve_headers_and_emit_nothing(
    settings, telemetry, method, path
):
    tracer, exporter = telemetry

    async def respond(request):
        assert request.headers["traceparent"] == "original"
        return httpx.Response(200, json={"total": 0})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://peer"
    ) as client:
        result = await ServiceClient(
            client, settings, uuid4(), [(b"traceparent", b"original")], tracer=tracer
        ).read(method, path, Result)
    assert result.total == 0
    assert not exporter.get_finished_spans()


def test_disabled_provider_does_not_initialize_exporter(settings):
    assert create_provider(settings) is None


def test_unsupported_sampler_is_not_silently_ignored(settings):
    with pytest.raises(ValueError, match=r"sampler|parentbased"):
        create_provider(
            settings.model_copy(update={"otel_enabled": True, "otel_traces_sampler": "other"})
        )
