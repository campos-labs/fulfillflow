"""HTTP tracing through the actual Core forwarding route; no SQL substitutes."""

import json

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from fulfillflow.http.telemetry import (
    INTERNAL_ROUTE,
    PROPAGATOR,
    PUBLIC_ROUTE,
    DiagnosticMiddleware,
    create_provider,
)
from fulfillflow.main import create_app


@pytest.fixture
def telemetry():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({"service.name": "test"}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("test"), exporter
    provider.shutdown()


def core_app(settings, tracer, client):
    # This forwarding route has no SQL dependency; the peer transport is explicit.
    app = create_app(settings)
    app.state.settings = settings
    app.state.http_tracer = tracer
    app.state.service_client = client
    return app


async def test_real_forwarding_chain_and_privacy(settings, telemetry):
    tracer, exporter = telemetry
    peer = FastAPI()
    peer.state.http_tracer = tracer
    peer.add_middleware(DiagnosticMiddleware)

    @peer.get(INTERNAL_ROUTE)
    async def result():
        return {"items": [], "total": 0, "page": 1, "page_size": 2}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=peer), base_url="http://peer"
    ) as inner:
        core = core_app(settings, tracer, inner)
        with tracer.start_as_current_span("observer", kind=SpanKind.CLIENT) as root:
            headers = {"baggage": "private=NEVER_EXPORT", "tracestate": "vendor=NEVER_EXPORT"}
            PROPAGATOR.inject(headers)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=core), base_url="http://core"
            ) as client:
                response = await client.get(
                    PUBLIC_ROUTE + "?external_event_id=NEVER_EXPORT&page_size=2", headers=headers
                )
    assert response.status_code == 200
    assert response.json()["total"] == 0
    spans = exporter.get_finished_spans()
    assert len(spans) == 4
    by_id = {s.context.span_id: s for s in spans}
    assert {s.context.trace_id for s in spans} == {root.get_span_context().trace_id}
    child = next(s for s in spans if s.kind == SpanKind.SERVER and s.name.endswith(INTERNAL_ROUTE))
    hop = by_id[child.parent.span_id]
    server = by_id[hop.parent.span_id]
    assert hop.kind == SpanKind.CLIENT
    assert server.name == "GET " + PUBLIC_ROUTE
    assert server.parent.span_id == root.get_span_context().span_id
    assert [e.name for e in hop.events] == ["response_received", "content_type_validated"]
    assert "NEVER_EXPORT" not in json.dumps([s.to_json() for s in spans])
    assert all(not s.context.trace_state for s in spans)


@pytest.mark.parametrize(
    "mode,category,status",
    [
        ("transport", "transport", None),
        ("content_type", "invalid_content_type", 200),
        ("remote", "remote_http_error", 503),
    ],
)
async def test_forwarding_failure_categories_keep_original_response(
    settings, telemetry, mode, category, status
):
    tracer, exporter = telemetry

    def respond(request):
        if mode == "transport":
            raise httpx.ConnectError("NEVER_EXPORT", request=request)
        return httpx.Response(
            status,
            content=b"NEVER_EXPORT",
            headers={
                "Content-Type": "text/plain"
                if mode == "content_type"
                else "application/problem+json"
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://peer"
    ) as inner:
        app = core_app(settings, tracer, inner)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://core"
        ) as client:
            response = await client.get(PUBLIC_ROUTE)
    assert response.status_code == 503
    hop = next(s for s in exporter.get_finished_spans() if s.kind == SpanKind.CLIENT)
    assert hop.attributes["error.type"] == category
    assert hop.attributes.get("http.response.status_code") == status
    assert hop.status.status_code == StatusCode.ERROR
    assert "NEVER_EXPORT" not in hop.to_json()
    assert not any(e.name == "exception" for e in hop.events)


async def test_json_schema_is_not_silently_added_to_forwarding(settings, telemetry):
    tracer, exporter = telemetry
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=b"not-json", headers={"Content-Type": "application/json"}
            )
        ),
        base_url="http://peer",
    ) as inner:
        app = core_app(settings, tracer, inner)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://core"
        ) as client:
            response = await client.get(PUBLIC_ROUTE)
    assert response.status_code == 200
    assert response.content == b"not-json"
    hop = next(s for s in exporter.get_finished_spans() if s.kind == SpanKind.CLIENT)
    assert [e.name for e in hop.events] == ["response_received", "content_type_validated"]
    assert "error.type" not in hop.attributes


async def test_disabled_forwarding_preserves_parent_and_emits_nothing(settings, telemetry):
    _, exporter = telemetry
    seen = []

    def respond(request):
        seen.append(request.headers["traceparent"])
        return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "page_size": 2})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://peer"
    ) as inner:
        app = core_app(settings, None, inner)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://core"
        ) as client:
            response = await client.get(PUBLIC_ROUTE, headers={"traceparent": "original"})
    assert response.status_code == 200
    assert seen == ["original"]
    assert not exporter.get_finished_spans()


def test_disabled_provider_does_not_initialize_exporter(settings):
    assert create_provider(settings) is None


def test_unsupported_sampler_is_not_silently_ignored(settings):
    with pytest.raises(ValueError, match="parentbased"):
        create_provider(
            settings.model_copy(update={"otel_enabled": True, "otel_traces_sampler": "other"})
        )
