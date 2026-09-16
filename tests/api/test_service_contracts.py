"""v1.1 real HTTP contracts and PostgreSQL commit-boundary recovery proofs."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from tests.service_pair import create_app
from tests.support import ContentionProbe, FixedClock, run_with_proven_contention

from fulfillflow.config import Settings
from fulfillflow.contracts.core import ApplyEventCommand
from fulfillflow.db import Database
from fulfillflow.shipments.public import ShipmentReceipts

pytestmark = pytest.mark.integration


def _headers(settings: Settings) -> dict[str, str]:
    return {"X-FulfillFlow-Internal-Token": settings.internal_api_secret.get_secret_value()}


async def _counts(core: Database, tracking: Database) -> tuple[int, int, int, int]:
    async with core.engine.connect() as connection:
        receipts = await connection.scalar(text("SELECT count(*) FROM tracking_event_receipts"))
        notifications = await connection.scalar(text("SELECT count(*) FROM notifications"))
    async with tracking.engine.connect() as connection:
        inbox = await connection.scalar(text("SELECT count(*) FROM carrier_event_inbox"))
        events = await connection.scalar(text("SELECT count(*) FROM tracking_events"))
    return receipts, notifications, inbox, events


async def test_internal_auth_precedes_body_parsing_and_public_docs_hide_internal_routes(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with app.router.lifespan_context(app):
        for target, path in (
            (app, "/internal/v1/tracking-events"),
            (app.state.tracking_app, "/internal/v1/tracking/carriers/carrier-alpha/events"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(target), base_url="http://test"
            ) as client:
                for headers in (
                    [],
                    [("X-FulfillFlow-Internal-Token", "wrong")],
                    list(_headers(postgres_settings).items()) * 2,
                ):
                    request_id = str(uuid4())
                    response = await client.post(
                        path,
                        content=b"{invalid",
                        headers=[
                            *headers,
                            ("Content-Type", "application/json"),
                            ("X-Request-ID", request_id),
                        ],
                    )
                    assert response.status_code == 401
                    assert response.json()["code"] == "INTERNAL_AUTHENTICATION_FAILED"
                    assert response.headers["X-Request-ID"] == request_id
        assert not any(path.startswith("/internal/") for path in app.openapi()["paths"])
        assert await _counts(postgres_database, postgres_tracking_database) == (0, 0, 0, 0)


async def test_no_sql_checkout_crosses_http_and_correlation_survives_forwarding(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    seen: list[tuple[str, str, str]] = []

    class CheckReleased(httpx.AsyncBaseTransport):
        def __init__(self, caller: Any, target: Any) -> None:
            self.caller = caller
            self.transport = httpx.ASGITransport(target, raise_app_exceptions=False)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            pool = self.caller.state.database.engine.pool
            assert pool.checkedout() == 0, "An SQL connection crossed the HTTP boundary"
            seen.append(
                (
                    request.url.path,
                    request.headers["X-Request-ID"],
                    request.headers.get("traceparent", ""),
                )
            )
            return await self.transport.handle_async_request(request)

    app.state.service_transport = CheckReleased(app, app.state.tracking_app)
    app.state.tracking_app.state.service_transport = CheckReleased(app.state.tracking_app, app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        shipment_id = await _create_shipment(
            client, reference="RELEASED-SQL", carrier_code="carrier-alpha", tracking_code="SQL-ONE"
        )
        request_id = str(uuid4())
        traceparent = "00-12345678901234567890123456789012-1234567890123456-01"
        client.headers.update({"X-Request-ID": request_id, "traceparent": traceparent})
        response = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="sql-one",
            raw_body=_alpha_body("sql-one", "SQL-ONE"),
        )
        assert response.status_code == 202
        for path in (
            f"/api/v1/shipments/{shipment_id}/tracking",
            "/api/v1/carrier-events",
            f"/api/v1/carrier-events/{response.json()['inbox_event_id']}",
            "/",
            f"/shipments/{shipment_id}",
            "/carrier-events",
        ):
            assert (await client.get(path)).status_code == 200
        assert not any(path == "/internal/v1/tracking-events" for path, _, _ in seen)
        assert all(
            identifier == request_id and trace == traceparent for _, identifier, trace in seen
        )


async def test_concurrent_core_receipts_preserve_original_result_and_outbox(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
    monkeypatch,
):
    from tests.async_flow import drain

    from fulfillflow.contracts.messages import decode_message
    from fulfillflow.core.message_handler import apply_command

    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    probe = ContentionProbe()
    original = ShipmentReceipts.claim

    async def claimed(self, command):
        await probe.record_backend_pid(self._session)
        result = await original(self, command)
        if result is None:
            await probe.hold_first_transaction()
        return result

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client, reference="RECEIPT", carrier_code="carrier-alpha", tracking_code="RECEIPT"
        )
        accepted = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="receipt",
            raw_body=_alpha_body("receipt", "RECEIPT"),
        )
        assert accepted.status_code == 202
        async with postgres_tracking_database.session() as session:
            envelope = decode_message(await session.scalar(text("SELECT body FROM message_outbox")))

        async def apply():
            async with postgres_database.session() as session, session.begin():
                await apply_command(session, envelope, fixed_clock)

        monkeypatch.setattr(ShipmentReceipts, "claim", claimed)
        await run_with_proven_contention(postgres_database, probe, apply, apply)
        monkeypatch.setattr(ShipmentReceipts, "claim", original)
        async with postgres_database.session() as session:
            before = (
                await session.execute(text("SELECT message_id, body FROM message_outbox"))
            ).one()
        fixed_clock.current += timedelta(seconds=20)
        # Actual transport redelivery reuses the already persisted receipt/outbox identity.
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        async with postgres_database.session() as session:
            assert (
                await session.execute(text("SELECT message_id, body FROM message_outbox"))
            ).one() == before
        assert await _counts(postgres_database, postgres_tracking_database) == (1, 1, 1, 1)
        assert (await client.get(accepted.headers["location"])).json()["result"][
            "result"
        ] == "APPLIED"


async def test_core_rejection_and_content_conflicts_replay_after_state_changes(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
):
    from pydantic import ValidationError

    from fulfillflow.contracts.problems import EventIdentityConflictError
    from fulfillflow.core.events import CoreEventService

    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    command = ApplyEventCommand(
        event_id=uuid4(),
        carrier_id=UUID("00000000-0000-4000-8000-000000000100"),
        external_event_id="core-missing",
        payload_sha256="0" * 64,
        tracking_code="CORE-MISSING",
        canonical_status="IN_TRANSIT",
        occurred_at=fixed_clock.now(),
        received_at=fixed_clock.now(),
        external_status="MOVING",
        description=None,
        location=None,
    )
    for invalid in (
        {"canonical_status": "CANCELLED"},
        {"tracking_code": " not-normalized "},
        {"occurred_at": "2026-09-09T12:00:00"},
        {"payload_sha256": "invalid"},
        {"external_event_id": "contains space"},
        {"unexpected_field": "forbidden"},
    ):
        with pytest.raises(ValidationError):
            ApplyEventCommand.model_validate(command.model_dump() | invalid)

    async def submit(value):
        async with postgres_database.session() as session:
            return await CoreEventService(session, fixed_clock).apply(value)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        original = await submit(command)
        assert original.kind == "rejected"
        await _create_shipment(
            client, reference="LATE", carrier_code="carrier-alpha", tracking_code="CORE-MISSING"
        )
        fixed_clock.current += timedelta(minutes=1)
        assert await submit(command) == original
        for updates in (
            {"payload_sha256": "1" * 64},
            {"received_at": fixed_clock.now()},
            {"canonical_status": "DELIVERED"},
            {"event_id": uuid4()},
            {"carrier_id": UUID("00000000-0000-4000-8000-000000000101")},
        ):
            with pytest.raises(EventIdentityConflictError):
                await submit(command.model_copy(update=updates))
        assert await _counts(postgres_database, postgres_tracking_database) == (1, 0, 0, 0)
