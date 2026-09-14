"""Real loopback HTTP cancellation after a durable Core commit; no benchmark traffic."""

import asyncio
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import text
from starlette.types import Message, Receive, Scope, Send
from tests.api.test_service_contracts import _counts
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from tests.service_pair import create_app
from tests.support import FixedClock

from fulfillflow.config import Settings
from fulfillflow.db import Database

pytestmark = pytest.mark.integration


class ResponseGate:
    """Hold actual response headers after the Core endpoint completed its transaction."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.hold_once = True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def gated_send(message: Message) -> None:
            if (
                scope["path"] == "/internal/v1/tracking-events"
                and message["type"] == "http.response.start"
                and self.hold_once
            ):
                assert message["status"] == 200
                self.hold_once = False
                self.reached.set()
                await self.release.wait()
            await send(message)

        await self.app(scope, receive, gated_send)


class TestServer(uvicorn.Server):
    __test__ = False

    def __init__(self, app: ResponseGate) -> None:
        super().__init__(uvicorn.Config(app, lifespan="off", log_config=None))
        self.ready = asyncio.Event()

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The test owns shutdown; do not replace the host pytest process's signals.
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        self.ready.set()


class ObservedHTTPTransport(httpx.AsyncHTTPTransport):
    """Record the real network interruption without manufacturing an exception."""

    def __init__(self) -> None:
        super().__init__()
        self.interruptions: list[type[BaseException]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await super().handle_async_request(request)
        except (httpx.ReadTimeout, asyncio.CancelledError) as exc:
            self.interruptions.append(type(exc))
            raise


@pytest.mark.parametrize("interruption", ["deadline", "caller_cancellation"])
async def test_real_http_interruption_after_core_commit_recovers_without_duplicate_effects(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    interruption: str,
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    settings = postgres_settings.model_copy(
        update={
            "core_base_url": f"http://127.0.0.1:{port}",
            "service_http_timeout_seconds": 6,
            "forwarding_timeout_seconds": 8,
        }
    )
    app = create_app(settings, postgres_database, clock=fixed_clock)
    # Only Tracking -> Core uses a real TCP socket. Public forwarding remains ASGI.
    network = ObservedHTTPTransport()
    app.state.tracking_app.state.service_transport = network
    gate = ResponseGate(app)
    server = TestServer(gate)
    serving: asyncio.Task[None] | None = None
    pending: asyncio.Task[httpx.Response] | None = None
    body = _alpha_body("http-recovery", "HTTP-RECOVERY", status="DELIVERED")
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
        ):
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            await asyncio.wait_for(server.ready.wait(), 5)
            await _create_shipment(
                client,
                reference="HTTP-RECOVERY",
                carrier_code="carrier-alpha",
                tracking_code="HTTP-RECOVERY",
            )

            async def post(raw: bytes = body) -> httpx.Response:
                return await _post_event(
                    client,
                    settings,
                    fixed_clock,
                    carrier_code="carrier-alpha",
                    event_id="http-recovery",
                    raw_body=raw,
                )

            pending = asyncio.create_task(post())
            await asyncio.wait_for(gate.reached.wait(), 5)
            # Independent SQL connections see the commit while HTTP is still pending.
            assert await _counts(postgres_database, postgres_tracking_database) == (1, 1, 1, 0)
            async with postgres_database.engine.connect() as connection:
                receipt_before = (
                    await connection.execute(text("SELECT * FROM tracking_event_receipts"))
                ).one()
            async with postgres_tracking_database.engine.connect() as connection:
                inbox_before = (
                    await connection.execute(
                        text("SELECT id, received_at, request_id, command FROM carrier_event_inbox")
                    )
                ).one()
            assert not pending.done()
            assert postgres_database.engine.pool.checkedout() == 0
            assert app.state.tracking_app.state.database.engine.pool.checkedout() == 0
            # A separate transaction can acquire the inbox lock before releasing HTTP.
            async with postgres_tracking_database.engine.begin() as connection:
                await connection.execute(
                    text("SELECT id FROM carrier_event_inbox FOR UPDATE NOWAIT")
                )

            if interruption == "deadline":
                response = await asyncio.wait_for(asyncio.shield(pending), 10)
                assert response.status_code == 503
                assert response.json()["code"] == "SERVICE_UNAVAILABLE"
            else:
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            assert len(network.interruptions) == 1
            # The total asyncio deadline can cancel HTTP before its read timeout.
            # Both are real interruptions; caller cancellation must stay CancelledError.
            if interruption == "deadline":
                assert network.interruptions[0] in (httpx.ReadTimeout, asyncio.CancelledError)
            else:
                assert network.interruptions[0] is asyncio.CancelledError
            assert not gate.release.is_set()
            assert await _counts(postgres_database, postgres_tracking_database) == (1, 1, 1, 0)
            async with postgres_tracking_database.engine.connect() as connection:
                assert await connection.scalar(text("SELECT status FROM carrier_event_inbox")) == (
                    "RECEIVED"
                )

            gate.release.set()
            fixed_clock.current += timedelta(seconds=20)
            recovered = await post()
            assert recovered.status_code == 200
            assert recovered.json()["result"] == "APPLIED"
            duplicate = await post()
            assert duplicate.status_code == 200
            assert duplicate.json()["result"] == "DUPLICATE"
            conflict = await post(body + b" ")
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "EVENT_ID_PAYLOAD_CONFLICT"
            assert await _counts(postgres_database, postgres_tracking_database) == (1, 1, 1, 1)
            async with postgres_database.engine.connect() as connection:
                assert (
                    await connection.execute(text("SELECT * FROM tracking_event_receipts"))
                ).one() == receipt_before
                assert await connection.scalar(text("SELECT status FROM orders")) == "FULFILLED"
                assert await connection.scalar(text("SELECT status FROM shipments")) == "DELIVERED"
            async with postgres_tracking_database.engine.connect() as connection:
                assert (
                    await connection.execute(
                        text("SELECT id, received_at, request_id, command FROM carrier_event_inbox")
                    )
                ).one() == inbox_before
                timeline = (
                    await connection.execute(
                        text("SELECT id, received_at, created_at FROM tracking_events")
                    )
                ).one()
                assert str(timeline.id) == receipt_before.result["event_id"]
                assert timeline.received_at == inbox_before.received_at
                assert timeline.created_at.isoformat() == receipt_before.result[
                    "decided_at"
                ].replace("Z", "+00:00")
    finally:
        gate.release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        server.should_exit = True
        if serving is not None:
            try:
                await asyncio.wait_for(asyncio.shield(serving), 5)
            finally:
                if not serving.done():
                    serving.cancel()
                    await asyncio.gather(serving, return_exceptions=True)
        listener.close()
