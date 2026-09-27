"""Opt-in spans for the bounded carrier-event HTTP diagnostic, with no payload capture."""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fulfillflow.config import Settings

PUBLIC_ROUTE = "/api/v1/carrier-events"
INTERNAL_ROUTE = "/internal/v1/tracking/carrier-events"
PROPAGATOR = TraceContextTextMapPropagator()


def create_provider(settings: Settings) -> TracerProvider | None:
    if not settings.otel_enabled:
        return None
    if settings.otel_traces_sampler != "parentbased_traceidratio":
        raise ValueError("HTTP diagnostic supports parentbased_traceidratio only")
    endpoint = str(settings.otel_exporter_otlp_endpoint).rstrip("/") + "/v1/traces"
    provider = TracerProvider(
        resource=Resource({"service.name": settings.otel_service_name}),
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_arg)),
        shutdown_on_exit=False,
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, timeout=2, headers={}),
            max_queue_size=128,
            max_export_batch_size=32,
            schedule_delay_millis=200,
            export_timeout_millis=3000,
        )
    )
    return provider


def tracer_for(provider: TracerProvider | None) -> Tracer | None:
    return provider.get_tracer("fulfillflow.http.diagnostic", "1") if provider else None


@contextmanager
def operation(tracer: Tracer | None, method: str, path: str) -> Iterator[Span | None]:
    if tracer is None or method != "GET" or path != INTERNAL_ROUTE:
        yield None
        return
    with tracer.start_as_current_span(
        "GET " + INTERNAL_ROUTE,
        kind=SpanKind.CLIENT,
        attributes={"http.request.method": "GET", "http.route": INTERNAL_ROUTE},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span


class DiagnosticMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        route = scope.get("path")
        tracer = (
            getattr(scope.get("app", None).state, "http_tracer", None) if scope.get("app") else None
        )
        if (
            scope["type"] != "http"
            or scope.get("method") != "GET"
            or route not in (PUBLIC_ROUTE, INTERNAL_ROUTE)
            or tracer is None
        ):
            await self.app(scope, receive, send)
            return
        # Extract only the W3C parent; baggage and tracestate are intentionally absent.
        parent = {
            k.decode("ascii"): v.decode("latin1")
            for k, v in scope["headers"]
            if k == b"traceparent"
        }
        with tracer.start_as_current_span(
            "GET " + route,
            context=PROPAGATOR.extract(parent),
            kind=SpanKind.SERVER,
            attributes={"http.request.method": "GET", "http.route": route},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:

            async def observed_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    span.set_attribute("http.response.status_code", message["status"])
                    if message["status"] >= 500:
                        span.set_status(StatusCode.ERROR)
                await send(message)

            try:
                await self.app(scope, receive, observed_send)
            except BaseException:
                span.set_attribute("error.type", "request_aborted")
                span.set_status(StatusCode.ERROR)
                raise
