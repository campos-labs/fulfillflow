"""Reverse result arrival preserves Core decisions and completes all accepted events."""

import os
from uuid import UUID

import aio_pika
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from tests.service_pair import create_app

from fulfillflow.contracts.messages import decode_message
from fulfillflow.core.message_handler import apply_command
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging.amqp import declare_flow, publish, receive
from fulfillflow.messaging.store import process_one
from fulfillflow.tracking.message_handler import apply_result
from fulfillflow.tracking.message_tables import tables as tracking_tables
from fulfillflow.tracking.operations import business_diagnostic


async def test_reverse_results_and_stale_command_preserve_business_decisions(
    postgres_database,
    postgres_tracking_database,
    postgres_settings,
    fixed_clock,
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client, reference="ORDERING", carrier_code="carrier-alpha", tracking_code="ORDERING"
        )
        accepted = []
        # The newer command is decided first; the older command must become stale.
        for name, status, when in (
            ("new", "DELIVERED", "2026-08-29T11:30:00Z"),
            ("old", "MOVING", "2026-08-29T10:30:00Z"),
        ):
            response = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id=name,
                raw_body=_alpha_body(name, "ORDERING", status=status, event_date=when),
            )
            assert response.status_code == 202
            accepted.append(UUID(response.json()["inbox_event_id"]))
        async with postgres_tracking_database.session() as session:
            report = await business_diagnostic(session, fixed_clock.now(), accepted[0])
            assert report["accepted_nonterminal"] == 2 and report["legacy_pending"] == 0
            commands = [
                decode_message(
                    await session.scalar(
                        select(tracking_tables.outbox.c.body).where(
                            tracking_tables.outbox.c.correlation_id == identity
                        )
                    )
                )
                for identity in accepted
            ]
        connection = await aio_pika.connect(os.environ["TEST_AMQP_URL"])
        async with connection:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            for flow in ("tracking.apply.v1", "tracking.result.v1"):
                await declare_flow(channel, flow)

            async def core_apply(session, message):
                await apply_command(session, message, fixed_clock)

            async def tracking_apply(session, message):
                await apply_result(session, message, fixed_clock)

            for message in commands:
                await publish(channel, message)
                queue = await channel.get_queue("tracking.apply.v1.queue")
                await receive(
                    postgres_database,
                    core_tables,
                    await queue.get(timeout=5),
                    message.type,
                    fixed_clock.now(),
                )
                async with postgres_database.session() as session, session.begin():
                    assert await process_one(
                        session, core_tables.inbox, fixed_clock.now(), core_apply
                    )
            async with postgres_database.session() as session:
                results = [
                    decode_message(
                        await session.scalar(
                            select(core_tables.outbox.c.body).where(
                                core_tables.outbox.c.correlation_id == identity
                            )
                        )
                    )
                    for identity in accepted
                ]
            assert results[0].payload.result.result.value == "APPLIED"
            assert results[1].payload.result.result.value == "IGNORED_STALE"
            for message in reversed(results):
                await publish(channel, message)
                queue = await channel.get_queue("tracking.result.v1.queue")
                await receive(
                    postgres_tracking_database,
                    tracking_tables,
                    await queue.get(timeout=5),
                    message.type,
                    fixed_clock.now(),
                )
                async with postgres_tracking_database.session() as session, session.begin():
                    assert await process_one(
                        session, tracking_tables.inbox, fixed_clock.now(), tracking_apply
                    )
        async with postgres_tracking_database.session() as session:
            report = await business_diagnostic(session, fixed_clock.now(), commands[0].event_id)
            assert report["accepted_nonterminal"] == 0
            assert report["items"][0]["result"] == results[0].payload.result.model_dump(mode="json")
            assert await session.scalar(text("SELECT count(*) FROM tracking_events")) == 2
        async with postgres_database.session() as session:
            assert await session.scalar(text("SELECT status FROM shipments")) == "DELIVERED"
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 1
            assert await session.scalar(text("SELECT status FROM orders")) == "FULFILLED"
