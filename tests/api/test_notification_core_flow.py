"""Atomic Core facts and eventual simulations are distinct observable journeys."""

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from tests.async_flow import drain, drain_notifications
from tests.service_pair import create_app

from fulfillflow.contracts.messages import NotificationEnvelope, decode_message
from fulfillflow.core import message_handler
from fulfillflow.messaging.store import RetryableItemError

pytestmark = pytest.mark.integration


async def test_core_and_tracking_complete_with_notifications_stopped_then_use_original_snapshot(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    postgres_notifications_database,
    fixed_clock,
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        shipment_id = await _create_shipment(
            client,
            reference="ASYNC-NOTIFICATION",
            carrier_code="carrier-alpha",
            tracking_code="ASYNC-NOTIFICATION",
        )
        body = _alpha_body("async-notification", "ASYNC-NOTIFICATION", status="DELIVERED")
        accepted = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="async-notification",
            raw_body=body,
        )
        assert accepted.status_code == 202
        await drain(
            postgres_database, postgres_tracking_database, fixed_clock, include_notifications=False
        )
        assert (await client.get(accepted.headers["location"])).json()["status"] == "PROCESSED"
        assert (await client.get(f"/api/v1/shipments/{shipment_id}")).json()[
            "status"
        ] == "DELIVERED"
        async with postgres_database.session() as session:
            assert await session.scalar(text("SELECT status FROM orders")) == "FULFILLED"
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
            fact_bytes = await session.scalar(
                text("SELECT body FROM message_outbox WHERE type='shipment.status_changed.v1'")
            )
            fact = decode_message(fact_bytes)
            assert isinstance(fact, NotificationEnvelope)
            recipient = await session.scalar(text("SELECT recipient_email FROM orders"))
            assert fact.payload.recipient == recipient
        async with postgres_notifications_database.session() as session:
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
        # The immutable outbox snapshot is sufficient even if mutable Core data changes later.
        async with postgres_database.session() as session, session.begin():
            await session.execute(text("UPDATE orders SET recipient_email='later@example.test'"))
        fixed_clock.current += timedelta(minutes=3)
        await drain_notifications(postgres_database, fixed_clock)
        notifications = (await client.get("/api/v1/notifications")).json()["items"]
        assert len(notifications) == 1 and notifications[0]["recipient"] == recipient
        assert notifications[0]["status"] == "SIMULATED"
        duplicate = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="async-notification",
            raw_body=body,
        )
        assert duplicate.status_code == 200 and duplicate.json()["result"] == "DUPLICATE"
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        assert (await client.get("/api/v1/notifications")).json()["items"] == notifications
        async with postgres_database.session() as session:
            assert (
                await session.scalar(
                    text("SELECT body FROM message_outbox WHERE type='shipment.status_changed.v1'")
                )
                == fact_bytes
            )
            assert await session.scalar(text("SELECT count(*) FROM message_outbox")) == 2


@pytest.mark.parametrize("failed_type", ["shipment.status_changed.v1", "tracking.result.v1"])
async def test_outbox_failure_rolls_back_all_core_effects_before_local_retry(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    postgres_notifications_database,
    fixed_clock,
    monkeypatch,
    failed_type,
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    original = message_handler.put_message

    async def fail_after_write(session, table, message, now):
        await original(session, table, message, now)
        if message.type == failed_type:
            raise RetryableItemError("synthetic outbox write failure")

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client,
            reference="OUTBOX-ATOMIC",
            carrier_code="carrier-alpha",
            tracking_code="OUTBOX-ATOMIC",
        )
        accepted = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="outbox-atomic",
            raw_body=_alpha_body("outbox-atomic", "OUTBOX-ATOMIC", status="DELIVERED"),
        )
        monkeypatch.setattr(message_handler, "put_message", fail_after_write)
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        async with postgres_database.session() as session:
            assert await session.scalar(text("SELECT count(*) FROM tracking_event_receipts")) == 0
            assert await session.scalar(text("SELECT count(*) FROM message_outbox")) == 0
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
            assert await session.scalar(text("SELECT status FROM shipments")) == "PENDING"
            assert await session.scalar(text("SELECT status FROM orders")) == "CONFIRMED"
            assert await session.scalar(text("SELECT state FROM message_inbox")) == "RETRY_WAIT"
        async with postgres_notifications_database.session() as session:
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
        assert (await client.get(accepted.headers["location"])).json()["status"] == "RECEIVED"
        monkeypatch.setattr(message_handler, "put_message", original)
        fixed_clock.current += timedelta(seconds=2)
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        assert (await client.get(accepted.headers["location"])).json()["status"] == "PROCESSED"
        assert (await client.get("/api/v1/notifications")).json()["total"] == 1


async def test_manual_creation_and_cancellation_do_not_publish_notification_facts(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        identifier = await _create_shipment(
            client,
            reference="MANUAL-NONE",
            carrier_code="carrier-alpha",
            tracking_code="MANUAL-NONE",
        )
        cancelled = await client.post(f"/api/v1/shipments/{identifier}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        async with postgres_database.session() as session:
            assert await session.scalar(text("SELECT count(*) FROM message_outbox")) == 0
        assert (await client.get("/api/v1/notifications")).json()["total"] == 0
