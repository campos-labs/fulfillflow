"""A fresh owner worker recovers actual acknowledged business work from PostgreSQL."""

import asyncio
import os

import aio_pika
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from fulfillflow.core.message_handler import apply_command
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging.amqp import declare_flow, publish_batch, receive
from fulfillflow.messaging.store import process_one
from fulfillflow.tracking.message_tables import tables as tracking_tables
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from tests.e2e.test_external_simulator_journey import _start_application, _stop_application
from tests.service_pair import create_app


@pytest.mark.parametrize("owner", ["core", "tracking"])
async def test_fresh_worker_recovers_acknowledged_work_with_empty_queue(
    postgres_settings,
    postgres_database,
    postgres_tracking_settings,
    postgres_tracking_database,
    fixed_clock,
    owner,
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    processes = []
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client, reference="RESTART", carrier_code="carrier-alpha", tracking_code="RESTART"
        )
        accepted = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="restart",
            raw_body=_alpha_body("restart", "RESTART", status="DELIVERED"),
        )
        assert accepted.status_code == 202
        connection = await aio_pika.connect(os.environ["TEST_AMQP_URL"], timeout=5)
        async with connection:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            for flow in ("tracking.apply.v1", "tracking.result.v1"):
                await declare_flow(channel, flow)
            await publish_batch(postgres_tracking_database, tracking_tables, channel, fixed_clock)
            command_queue = await channel.get_queue("tracking.apply.v1.queue")
            await receive(
                postgres_database,
                core_tables,
                await command_queue.get(timeout=5),
                "tracking.apply.v1",
                fixed_clock.now(),
            )
            if owner == "tracking":

                async def apply(session, envelope):
                    await apply_command(session, envelope, fixed_clock)

                async with postgres_database.session() as session, session.begin():
                    assert await process_one(session, core_tables.inbox, fixed_clock.now(), apply)
                await publish_batch(
                    postgres_database, core_tables, channel, fixed_clock, flow="tracking.result.v1"
                )
                result_queue = await channel.get_queue("tracking.result.v1.queue")
                await receive(
                    postgres_tracking_database,
                    tracking_tables,
                    await result_queue.get(timeout=5),
                    "tracking.result.v1",
                    fixed_clock.now(),
                )
            queue = await channel.get_queue(
                f"tracking.{'apply' if owner == 'core' else 'result'}.v1.queue"
            )
            assert queue.declaration_result.message_count == 0
        target = postgres_database if owner == "core" else postgres_tracking_database
        async with target.session() as session:
            assert await session.scalar(text("SELECT state FROM message_inbox")) == "PENDING"
        assert (await client.get(accepted.headers["location"])).json()["status"] == "RECEIVED"
        try:
            # These new OS processes have no memory of reception; only durable rows remain.
            for settings in (
                (postgres_settings, postgres_tracking_settings)
                if owner == "core"
                else (postgres_tracking_settings,)
            ):
                environment = dict(os.environ)
                environment.update(
                    APP_ENV="test",
                    SERVICE_ROLE=settings.service_role,
                    DATABASE_URL=settings.database_dsn,
                    AMQP_URL=os.environ["TEST_AMQP_URL"],
                    INTERNAL_API_SECRET=settings.internal_api_secret.get_secret_value(),
                    SESSION_SECRET=settings.session_secret.get_secret_value(),
                    CARRIER_ALPHA_WEBHOOK_SECRET=postgres_tracking_settings.carrier_alpha_webhook_secret.get_secret_value(),
                    CARRIER_BETA_WEBHOOK_SECRET=postgres_tracking_settings.carrier_beta_webhook_secret.get_secret_value(),
                )
                processes.append(
                    _start_application(environment, f"fulfillflow.{settings.service_role}.worker")
                )
            for _ in range(100):
                assert all(process.poll() is None for process in processes), "worker exited"
                detail = (await client.get(accepted.headers["location"])).json()
                if detail["status"] == "PROCESSED":
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail(f"Durable work was not recovered: {detail['progress']}")
            assert detail["result"]["current_status"] == "DELIVERED"
            async with postgres_database.session() as session:
                assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM message_outbox "
                            "WHERE type = 'shipment.status_changed.v1'"
                        )
                    )
                    == 1
                )
                assert (
                    await session.scalar(text("SELECT count(*) FROM tracking_event_receipts")) == 1
                )
                assert await session.scalar(text("SELECT status FROM orders")) == "FULFILLED"
            async with postgres_tracking_database.session() as session:
                assert await session.scalar(text("SELECT count(*) FROM tracking_events")) == 1
                assert await session.scalar(text("SELECT state FROM message_inbox")) == "DONE"
        finally:
            for process in reversed(processes):
                await asyncio.to_thread(_stop_application, process)
