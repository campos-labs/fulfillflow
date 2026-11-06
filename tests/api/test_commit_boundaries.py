"""Faults at actual SQL commit hooks, followed by redelivery over the public API."""

from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from tests.api.test_service_contracts import _counts
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from tests.async_flow import drain
from tests.service_pair import create_app
from tests.support import FixedClock

from fulfillflow.config import Settings
from fulfillflow.contracts.messages import decode_message
from fulfillflow.core.message_handler import apply_command
from fulfillflow.db import Database
from fulfillflow.messaging.store import BlockedItemError
from fulfillflow.shipments.public import ShipmentReceipts
from fulfillflow.tracking.repository import TrackingRepository

pytestmark = pytest.mark.integration


async def test_reconciliation_reads_real_owner_databases_and_rejects_receipt_corruption(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client,
            reference="RECONCILE",
            carrier_code="carrier-alpha",
            tracking_code="RECONCILE-ONE",
        )
        response = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="reconcile-event",
            raw_body=_alpha_body("reconcile-event", "RECONCILE-ONE"),
        )
        assert response.status_code == 202
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        completed = (await client.get(response.headers["location"])).json()
        notifications = (await client.get("/api/v1/notifications")).json()["items"]
        assert len(notifications) == 1
        assert notifications[0]["tracking_event_id"] == completed["tracking_event_id"]
        assert notifications[0]["shipment_id"] == completed["result"]["shipment_id"]
        async with postgres_database.session() as session:
            receipt = await ShipmentReceipts(session).get(UUID(completed["tracking_event_id"]))
        assert receipt.model_dump(mode="json") == completed["result"]
        async with postgres_tracking_database.session() as session:
            envelope = decode_message(await session.scalar(text("SELECT body FROM message_outbox")))
        async with postgres_database.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tracking_event_receipts SET result = "
                    "jsonb_set(result, '{current_status}', '\"DELIVERED\"')"
                )
            )
        with pytest.raises(BlockedItemError, match="RESULT_OUTBOX_CONFLICT"):
            async with postgres_database.session() as session, session.begin():
                await apply_command(session, envelope, fixed_clock)
        assert (await client.get("/api/v1/notifications")).json()["items"] == notifications


@pytest.mark.parametrize("peer", ["core", "tracking"])
async def test_peer_unavailability_preserves_durable_state_and_allows_redelivery(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    peer: str,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    target = app if peer == "tracking" else app.state.tracking_app
    original_transport = target.state.service_transport

    class Unavailable(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if peer in ("tracking", "core"):
                raise httpx.ConnectError("synthetic peer outage", request=request)
            return await original_transport.handle_async_request(request)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client, reference="OUTAGE", carrier_code="carrier-alpha", tracking_code="OUTAGE-ONE"
        )
        service_client = target.state.service_client
        original = service_client._transport
        service_client._transport = Unavailable()

        async def post() -> httpx.Response:
            return await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="outage-event",
                raw_body=_alpha_body("outage-event", "OUTAGE-ONE"),
            )

        try:
            assert (await post()).status_code == 503
            assert await _counts(postgres_database, postgres_tracking_database) == (
                0,
                0,
                0,
                0,
            )
        finally:
            service_client._transport = original
        assert (await post()).status_code == 202
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        assert await _counts(postgres_database, postgres_tracking_database) == (1, 1, 1, 1)


@pytest.mark.parametrize("boundary", ["admission", "core", "finalization"])
@pytest.mark.parametrize("when", ["before_commit", "after_commit"])
async def test_commit_interruption_preserves_atomic_stage_and_recovers_locally(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
    monkeypatch,
    boundary,
    when,
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    fired = False

    def interrupt(session):
        nonlocal fired
        if session.in_nested_transaction():
            return
        if session.info.pop("commit_boundary", None) == boundary and not fired:
            fired = True
            raise SQLAlchemyError("synthetic commit interruption")

    original_save = TrackingRepository.save_inbox
    original_finalize = ShipmentReceipts.finalize

    async def save(self, inbox):
        await original_save(self, inbox)
        self._session.info["commit_boundary"] = (
            "admission" if inbox.status.value == "RECEIVED" else "finalization"
        )

    async def finalize(self, command, result):
        await original_finalize(self, command, result)
        self._session.info["commit_boundary"] = "core"

    monkeypatch.setattr(TrackingRepository, "save_inbox", save)
    monkeypatch.setattr(ShipmentReceipts, "finalize", finalize)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://test"
        ) as client,
    ):
        await _create_shipment(
            client, reference="COMMIT", carrier_code="carrier-alpha", tracking_code="COMMIT-ONE"
        )

        async def post():
            return await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="commit-event",
                raw_body=_alpha_body("commit-event", "COMMIT-ONE", status="DELIVERED"),
            )

        event.listen(Session, when, interrupt)
        try:
            accepted = await post()
            if boundary == "admission":
                assert accepted.status_code == 503
            else:
                assert accepted.status_code == 202
                with pytest.raises(SQLAlchemyError):
                    await drain(postgres_database, postgres_tracking_database, fixed_clock)
        finally:
            event.remove(Session, when, interrupt)
        assert fired
        committed = ["admission", "core", "finalization"].index(boundary) + int(
            when == "after_commit"
        )
        assert await _counts(postgres_database, postgres_tracking_database) == (
            int(committed >= 2),
            0,  # Notifications is a separate journey and has not been drained after this fault.
            int(committed >= 1),
            int(committed >= 3),
        )
        async with postgres_tracking_database.session() as session:
            original = (
                await session.execute(
                    text("SELECT id, received_at, request_id, command FROM carrier_event_inbox")
                )
            ).first()
            assert await session.scalar(text("SELECT count(*) FROM message_outbox")) == int(
                committed >= 1
            )
            if original:
                assert original.command is not None
        async with postgres_database.session() as session:
            assert await session.scalar(text("SELECT count(*) FROM message_outbox")) == 2 * int(
                committed >= 2
            )
        # Only uncertain admission needs redelivery. Acknowledged application work resumes locally.
        if boundary == "admission":
            assert (await post()).status_code == 202
        fixed_clock.current += timedelta(seconds=31)
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        assert await _counts(postgres_database, postgres_tracking_database) == (1, 1, 1, 1)
        assert (await post()).json()["result"] == "DUPLICATE"
        async with postgres_tracking_database.session() as session:
            final = (
                await session.execute(
                    text("SELECT id, received_at, request_id, command FROM carrier_event_inbox")
                )
            ).one()
            if original:
                assert final == original
            assert await session.scalar(text("SELECT state FROM message_inbox")) == "DONE"
        async with postgres_database.session() as session:
            assert await session.scalar(text("SELECT status FROM orders")) == "FULFILLED"
            assert await session.scalar(text("SELECT state FROM message_inbox")) == "DONE"


@pytest.mark.parametrize("owner", ["tracking", "core"])
async def test_outbox_insert_failure_rolls_back_the_entire_local_fact(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
    monkeypatch,
    owner,
):
    from fulfillflow.core import message_handler
    from fulfillflow.messaging.store import RetryableItemError
    from fulfillflow.tracking import service

    module = service if owner == "tracking" else message_handler
    original = module.put_message

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        if owner == "tracking":
            raise SQLAlchemyError("injected outbox failure")
        raise RetryableItemError("injected outbox failure")

    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://test"
        ) as client,
    ):
        await _create_shipment(
            client, reference="OUTBOX", carrier_code="carrier-alpha", tracking_code="OUTBOX"
        )

        async def post():
            return await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="outbox",
                raw_body=_alpha_body("outbox", "OUTBOX", status="DELIVERED"),
            )

        monkeypatch.setattr(module, "put_message", fail)
        response = await post()
        if owner == "tracking":
            assert response.status_code == 503
            assert await _counts(postgres_database, postgres_tracking_database) == (0, 0, 0, 0)
        else:
            assert response.status_code == 202
            await drain(postgres_database, postgres_tracking_database, fixed_clock)
            assert await _counts(postgres_database, postgres_tracking_database) == (0, 0, 1, 0)
            async with postgres_database.session() as session:
                assert await session.scalar(text("SELECT state FROM message_inbox")) == "RETRY_WAIT"
                assert await session.scalar(text("SELECT status FROM orders")) == "CONFIRMED"
                assert await session.scalar(text("SELECT status FROM shipments")) == "PENDING"
        database = postgres_tracking_database if owner == "tracking" else postgres_database
        async with database.session() as session:
            assert await session.scalar(text("SELECT count(*) FROM message_outbox")) == 0
        monkeypatch.setattr(module, "put_message", original)
        if owner == "tracking":
            assert (await post()).status_code == 202
        fixed_clock.current += timedelta(seconds=2)
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        assert await _counts(postgres_database, postgres_tracking_database) == (1, 1, 1, 1)
