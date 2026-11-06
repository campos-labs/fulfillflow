"""Exercise opt-in tracing through actual service lifecycles and PostgreSQL owners."""

import httpx
import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from tests.service_pair import create_app

from fulfillflow.http.telemetry import INTERNAL_ROUTE, PUBLIC_ROUTE

pytestmark = pytest.mark.integration


async def test_real_query_preserves_result_and_owner_lifecycles(
    postgres_settings, postgres_database, fixed_clock, monkeypatch
):
    exporter = InMemorySpanExporter()

    def provider_for(settings):
        provider = TracerProvider(
            resource=Resource({"service.name": settings.service_role}), shutdown_on_exit=False
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        return provider

    monkeypatch.setattr("fulfillflow.main.create_provider", provider_for)
    monkeypatch.setattr("fulfillflow.tracking.app.create_provider", provider_for)
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://core"
        ) as client:
            result = await client.get(PUBLIC_ROUTE, params={"page": 1, "page_size": 2})
            assert result.status_code == 200
            assert result.json() == {"items": [], "total": 0, "page": 1, "page_size": 2}
        assert app.state.database.engine.pool.checkedout() == 0
        assert app.state.tracking_app.state.database.engine.pool.checkedout() == 0
    assert app.state.http_tracer is None
    assert app.state.tracking_app.state.http_tracer is None
    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    tracking = next(s for s in spans if s.resource.attributes["service.name"] == "tracking")
    hop = next(s for s in spans if s.kind == SpanKind.CLIENT)
    core = next(s for s in spans if s.name == "GET " + PUBLIC_ROUTE)
    assert tracking.name == hop.name == "GET " + INTERNAL_ROUTE
    assert tracking.parent.span_id == hop.context.span_id
    assert hop.parent.span_id == core.context.span_id
    assert all(s.context.trace_id == core.context.trace_id for s in spans)
    assert [e.name for e in hop.events] == ["response_received", "content_type_validated"]
