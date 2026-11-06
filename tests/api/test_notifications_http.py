"""Notifications read boundary and separate progress over three real owner databases."""

from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, select, text, update
from tests.api.test_notifications import _alpha_body, _create_shipment, _post_alpha_event
from tests.async_flow import drain, drain_notifications
from tests.integration.test_notifications_owned import import_records, legacy_record
from tests.service_pair import create_app

from fulfillflow.contracts.messages import decode_message
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging.store import put_message
from fulfillflow.notifications.app import create_app as create_notifications
from fulfillflow.notifications.message_tables import tables as notification_tables

pytestmark = pytest.mark.integration


async def applied(client, settings, core, tracking, clock):
    shipment_id = await _create_shipment(client, reference="ASYNC-HTTP", tracking_code="ASYNC-HTTP")
    admitted = await _post_alpha_event(
        client,
        settings,
        clock,
        event_id="async-http",
        raw_body=_alpha_body(
            "async-http", "ASYNC-HTTP", status="DELIVERED", event_date="2026-08-29T10:00:00Z"
        ),
    )
    assert admitted.status_code == 202
    await drain(core, tracking, clock, include_notifications=False)
    completed = (await client.get(admitted.headers["location"])).json()
    assert completed["result"]["result"] == "APPLIED"
    return completed["result"]["event_id"], shipment_id


async def test_notification_api_authentication_health_and_read_only_contract(
    postgres_notifications_settings,
    postgres_notifications_database,
    fixed_clock,
):
    app = create_notifications(
        postgres_notifications_settings, postgres_notifications_database, clock=fixed_clock
    )
    secret = postgres_notifications_settings.internal_api_secret.get_secret_value()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://notifications",
        ) as client,
    ):
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200
        for headers in ({}, {"X-FulfillFlow-Internal-Token": "wrong"}):
            response = await client.get("/internal/v1/notifications", headers=headers)
            assert response.status_code == 401
            assert response.json()["code"] == "INTERNAL_AUTHENTICATION_FAILED"
        headers = {"X-FulfillFlow-Internal-Token": secret, "X-Request-ID": str(UUID(int=55))}
        listed = await client.get("/internal/v1/notifications", headers=headers)
        assert listed.json() == {"items": [], "page": 1, "page_size": 25, "total": 0}
        assert listed.headers["x-request-id"] == str(UUID(int=55))
        assert "set-cookie" not in listed.headers
        counts = await client.get("/internal/v1/notification-counts", headers=headers)
        assert counts.json()["simulated"] == counts.json()["failed"] == 0
        unknown = await client.get(
            f"/internal/v1/notification-status/{UUID(int=1)}", headers=headers
        )
        assert unknown.json()["processing"] == "NOT_RECEIVED"
        assert unknown.json()["origin"] is None
        assert (await client.post("/internal/v1/notifications", headers=headers)).status_code == 405
        assert (await client.get("/api/v1/notifications", headers=headers)).status_code == 404


async def test_tracking_completion_precedes_notification_with_independent_query_and_ui(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    postgres_notifications_database,
    fixed_clock,
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
        ) as client,
    ):
        event_id, shipment_id = await applied(
            client, postgres_settings, postgres_database, postgres_tracking_database, fixed_clock
        )
        status_path = f"/api/v1/notification-status/{event_id}"
        pending = await client.get(status_path)
        assert pending.status_code == 200, pending.text
        assert pending.json()["required"] is True
        assert pending.json()["publication"] == "PENDING"
        assert pending.json()["processing"] == "NOT_RECEIVED"
        assert pending.json()["origin"] == "ASYNC"
        assert pending.json()["notification_id"] is None
        assert (await client.get("/api/v1/notifications")).json()["total"] == 0
        assert (
            "simulation is pending" in (await client.get(f"/notification-status/{event_id}")).text
        )
        assert (await client.get(f"/api/v1/shipments/{shipment_id}")).json()[
            "status"
        ] == "DELIVERED"
        async with postgres_database.session() as session:
            assert await session.scalar(text("SELECT status FROM orders")) == "FULFILLED"
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
        await drain_notifications(postgres_database, fixed_clock)
        finished = (await client.get(status_path)).json()
        assert finished["processing"] == "DONE"
        assert finished["status"] == "SIMULATED"
        assert finished["notification_id"]
        assert "Simulation recorded" in (await client.get(f"/notification-status/{event_id}")).text
        assert (await client.get("/api/v1/notifications")).json()["total"] == 1
        # Lost confirms may leave local publication pending after the terminal exists.
        async with postgres_database.session() as session, session.begin():
            await session.execute(
                update(core_tables.outbox)
                .where(core_tables.outbox.c.type == "shipment.status_changed.v1")
                .values(state="PENDING")
            )
        observed = (await client.get(status_path)).json()
        assert observed["publication"] == "PENDING"
        assert observed["status"] == "SIMULATED"


@pytest.mark.parametrize("state", ["PENDING", "RETRY_WAIT", "BLOCKED"])
async def test_progress_reports_durable_notifications_work(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    postgres_notifications_database,
    fixed_clock,
    state,
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
        ) as client,
    ):
        event_id, _ = await applied(
            client, postgres_settings, postgres_database, postgres_tracking_database, fixed_clock
        )
        async with postgres_database.session() as session:
            body = await session.scalar(
                select(core_tables.outbox.c.body).where(
                    core_tables.outbox.c.type == "shipment.status_changed.v1"
                )
            )
        async with postgres_notifications_database.session() as session, session.begin():
            await put_message(
                session, notification_tables.inbox, decode_message(body), fixed_clock.now()
            )
            await session.execute(update(notification_tables.inbox).values(state=state))
        response = await client.get(f"/api/v1/notification-status/{event_id}")
        assert response.status_code == 200, response.text
        assert response.json()["processing"] == state
        assert response.json()["notification_id"] is None
        if state == "BLOCKED":
            assert (
                "requires audited recovery"
                in (await client.get(f"/notification-status/{event_id}")).text
            )


async def test_notifications_unavailable_preserves_core_health_and_dashboard(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
):
    calls = []

    def unavailable(request):
        assert postgres_database.engine.pool.checkedout() == 0
        calls.append(request)
        raise httpx.ConnectError("private connection diagnostic", request=request)

    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    app.state.notifications_transport = httpx.MockTransport(unavailable)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
        ) as client,
    ):
        event_id, _ = await applied(
            client, postgres_settings, postgres_database, postgres_tracking_database, fixed_clock
        )
        for path in (
            "/api/v1/notifications",
            f"/api/v1/notifications/{UUID(int=1)}",
            f"/api/v1/notification-status/{event_id}",
        ):
            response = await client.get(path)
            assert response.status_code == 503, response.text
            assert response.json()["code"] == "SERVICE_UNAVAILABLE"
            assert "private" not in response.text
        assert (await client.get("/notifications")).status_code == 503
        dashboard = await client.get("/")
        assert dashboard.status_code == 200, dashboard.text
        assert "Notifications counts are unavailable" in dashboard.text
        assert "FULFILLED" in dashboard.text
        assert (await client.get("/health/ready")).status_code == 200
        assert (await client.get("/api/v1/orders")).status_code == 200
    assert len(calls) == 5


async def test_no_change_and_missing_receipts_do_not_query_notifications(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
):
    def forbidden(request):
        raise AssertionError("This Core observation requires no Notifications request")

    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    app.state.notifications_transport = httpx.MockTransport(forbidden)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
        ) as client,
    ):
        await applied(
            client, postgres_settings, postgres_database, postgres_tracking_database, fixed_clock
        )
        admitted = await _post_alpha_event(
            client,
            postgres_settings,
            fixed_clock,
            event_id="no-change-http",
            raw_body=_alpha_body(
                "no-change-http",
                "ASYNC-HTTP",
                status="DELIVERED",
                event_date="2026-08-29T10:01:00Z",
            ),
        )
        assert admitted.status_code == 202
        await drain(
            postgres_database, postgres_tracking_database, fixed_clock, include_notifications=False
        )
        result = (await client.get(admitted.headers["location"])).json()["result"]
        assert result["result"] == "NO_STATE_CHANGE"
        observed = await client.get(f"/api/v1/notification-status/{result['event_id']}")
        assert observed.status_code == 200, observed.text
        assert observed.json()["required"] is False
        for field in (
            "publication",
            "processing",
            "notification_id",
            "status",
            "origin",
            "simulated_at",
            "notifications_observed_at",
        ):
            assert observed.json()[field] is None
        missing = await client.get(f"/api/v1/notification-status/{UUID(int=77)}")
        assert missing.status_code == 404
        assert missing.json()["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize("terminal", [False, True])
async def test_missing_async_outbox_is_integrity_failure_not_legacy(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
    terminal,
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
        ) as client,
    ):
        event_id, _ = await applied(
            client, postgres_settings, postgres_database, postgres_tracking_database, fixed_clock
        )
        if terminal:
            await drain_notifications(postgres_database, fixed_clock)
        async with postgres_database.session() as session, session.begin():
            await session.execute(
                delete(core_tables.outbox).where(
                    core_tables.outbox.c.type == "shipment.status_changed.v1"
                )
            )
        observed = await client.get(f"/api/v1/notification-status/{event_id}")
        assert observed.status_code == 503, observed.text
        assert observed.json()["code"] == "SERVICE_UNAVAILABLE"


async def test_legacy_progress_requires_imported_record_and_preserves_original_identity(
    postgres_settings,
    postgres_database,
    postgres_notifications_database,
    fixed_clock,
):
    record = legacy_record()
    await import_records(postgres_notifications_database, record)
    async with postgres_database.session() as session, session.begin():
        await session.execute(
            text("""INSERT INTO tracking_event_receipts
            (event_id,carrier_id,external_event_id,content_sha256,result,created_at)
            VALUES (:event,'00000000-0000-4000-8000-000000000100','legacy-progress',:hash,
            CAST(:result AS jsonb),:now)"""),
            dict(
                event=record.tracking_event_id,
                hash="a" * 64,
                now=fixed_clock.now(),
                result='{"kind":"applied","event_id":"'
                + str(record.tracking_event_id)
                + '","shipment_id":"'
                + str(record.shipment_id)
                + '","result":"APPLIED",'
                '"previous_status":"PENDING","current_status":"POSTED",'
                '"decided_at":"2026-08-28T12:00:00Z"}',
            ),
        )
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.get(f"/api/v1/notification-status/{record.tracking_event_id}")
        assert response.status_code == 200, response.text
        assert response.json()["origin"] == "LEGACY"
        assert response.json()["publication"] is None
        assert response.json()["processing"] is None
        assert response.json()["notification_id"] == str(record.id)
        assert (await client.get(f"/api/v1/notifications/{record.id}")).json() == record.model_dump(
            mode="json"
        )

        legacy_page = await client.get(f"/notification-status/{record.tracking_event_id}")
        assert "Not recorded for LEGACY" in legacy_page.text
        assert "Imported LEGACY record" in legacy_page.text
        assert 'data-observation-poll="false"' in legacy_page.text
        assert 'hx-trigger="observe"' not in legacy_page.text
