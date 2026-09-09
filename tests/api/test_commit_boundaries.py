"""Faults at actual SQL commit hooks, followed by redelivery over the public API."""

import asyncio
import os
from datetime import timedelta
from typing import Any

import httpx
import pytest
from benchmarks.collectors import ExternalCommandError
from benchmarks.collectors_v11 import SplitDatabaseProbe
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from tests.api.test_service_contracts import LostResponse, _counts
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from tests.service_pair import create_app
from tests.support import ContentionProbe, FixedClock, run_with_proven_contention

from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.shipments.public import ShipmentReceipts
from fulfillflow.tracking.repository import TrackingRepository

pytestmark = pytest.mark.integration


async def test_reconciliation_reads_real_owner_databases_and_rejects_receipt_corruption(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
) -> None:
    container = os.environ.get("TEST_V11_POSTGRES_CONTAINER")
    if container is None:
        pytest.skip(
            "TEST_V11_POSTGRES_CONTAINER must identify the dedicated owner PostgreSQL container"
        )
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    probe = SplitDatabaseProbe(container)
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
        assert response.status_code == 200
        assert await asyncio.to_thread(probe.reconcile) == {
            "commands": 1,
            "receipts": 1,
            "finalized": 1,
        }
        observation = (await asyncio.to_thread(probe.event_observations, ["reconcile-event"]))[
            "reconcile-event"
        ]
        assert observation.raw_body_sha256 == observation.payload_sha256
        assert observation.notification_count == observation.matching_notification_count == 1
        async with postgres_database.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tracking_event_receipts SET result = "
                    "jsonb_set(result, '{current_status}', '\"DELIVERED\"')"
                )
            )
        with pytest.raises(ExternalCommandError, match="receipt differs"):
            await asyncio.to_thread(probe.reconcile)


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
            if peer == "tracking" or request.url.path == "/internal/v1/tracking-events":
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
                int(peer == "core"),
                0,
            )
        finally:
            service_client._transport = original
        assert (await post()).status_code == 200
        assert await _counts(postgres_database, postgres_tracking_database) == (1, 1, 1, 1)


@pytest.mark.parametrize("boundary", ["reception", "command", "core", "finalization"])
@pytest.mark.parametrize("when", ["before_commit", "after_commit"])
async def test_commit_interruption_preserves_exact_durable_prefix_and_recovers(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    when: str,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    fired = False

    def interrupt(session: Session) -> None:
        nonlocal fired
        if session.info.pop("commit_boundary", None) == boundary and not fired:
            fired = True
            raise SQLAlchemyError("synthetic connection interruption at commit")

    original_add = TrackingRepository.add_inbox
    original_save = TrackingRepository.save_inbox
    original_finalize = ShipmentReceipts.finalize

    async def add(self: TrackingRepository, inbox: Any) -> None:
        await original_add(self, inbox)
        self._session.info["commit_boundary"] = "reception"

    async def save(self: TrackingRepository, inbox: Any) -> None:
        await original_save(self, inbox)
        self._session.info["commit_boundary"] = (
            "command" if inbox.status.value == "RECEIVED" else "finalization"
        )

    async def finalize(self: ShipmentReceipts, command: Any, result: Any) -> None:
        await original_finalize(self, command, result)
        self._session.info["commit_boundary"] = "core"

    monkeypatch.setattr(TrackingRepository, "add_inbox", add)
    monkeypatch.setattr(TrackingRepository, "save_inbox", save)
    monkeypatch.setattr(ShipmentReceipts, "finalize", finalize)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client, reference="COMMIT", carrier_code="carrier-alpha", tracking_code="COMMIT-ONE"
        )

        async def post() -> httpx.Response:
            return await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="commit-event",
                raw_body=_alpha_body("commit-event", "COMMIT-ONE"),
            )

        event.listen(Session, when, interrupt)
        try:
            failed = await post()
        finally:
            event.remove(Session, when, interrupt)
        assert fired
        assert failed.status_code == 503
        committed = ["reception", "command", "core", "finalization"].index(boundary)
        if when == "after_commit":
            committed += 1
        assert await _counts(postgres_database, postgres_tracking_database) == (
            int(committed >= 3),
            int(committed >= 3),
            int(committed >= 1),
            int(committed >= 4),
        )
        async with postgres_tracking_database.engine.connect() as connection:
            before = (
                await connection.execute(
                    text("SELECT id, received_at, request_id, command FROM carrier_event_inbox")
                )
            ).first()
        if before is not None:
            assert (before.command is not None) == (committed >= 2)
        fixed_clock.current += timedelta(seconds=1)
        resumed = await post()
        assert resumed.status_code == 200
        assert resumed.json()["result"] == ("DUPLICATE" if committed == 4 else "APPLIED")
        assert await _counts(postgres_database, postgres_tracking_database) == (1, 1, 1, 1)
        async with postgres_tracking_database.engine.connect() as connection:
            after = (
                await connection.execute(
                    text("SELECT id, received_at, request_id, command FROM carrier_event_inbox")
                )
            ).one()
        if before is not None:
            assert after[:3] == before[:3]
            if before.command is not None:
                assert after.command == before.command


async def test_lost_rejection_response_replays_original_decision_after_shipment_creation(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    app.state.tracking_app.state.service_transport = LostResponse(app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):

        async def post() -> httpx.Response:
            return await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="lost-rejection",
                raw_body=_alpha_body("lost-rejection", "MISSING-ONE"),
            )

        assert (await post()).status_code == 503
        assert await _counts(postgres_database, postgres_tracking_database) == (1, 0, 1, 0)
        async with postgres_database.engine.connect() as connection:
            original = await connection.scalar(text("SELECT result FROM tracking_event_receipts"))
        await _create_shipment(
            client,
            reference="CREATED-LATER",
            carrier_code="carrier-alpha",
            tracking_code="MISSING-ONE",
        )
        fixed_clock.current += timedelta(seconds=5)
        first, second = await post(), await post()
        assert first.status_code == second.status_code == 422
        assert first.json()["code"] == second.json()["code"] == original["code"]
        assert await _counts(postgres_database, postgres_tracking_database) == (1, 0, 1, 0)
        async with postgres_database.engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT result FROM tracking_event_receipts"))
                == original
            )
            assert await connection.scalar(text("SELECT status FROM shipments")) == "PENDING"
        async with postgres_tracking_database.engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT status, processed_at FROM carrier_event_inbox")
                )
            ).one()
            assert row.status == "REJECTED"
            assert row.processed_at.isoformat() == original["decided_at"].replace("Z", "+00:00")


@pytest.mark.parametrize("fault", ["rollback", "lost_response"])
async def test_concurrent_redelivery_with_core_failure_has_one_durable_effect(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    if fault == "lost_response":
        app.state.tracking_app.state.service_transport = LostResponse(app)
    probe = ContentionProbe()
    original = ShipmentReceipts.claim
    failed = False

    async def claim(self: ShipmentReceipts, command: Any) -> Any:
        nonlocal failed
        await probe.record_backend_pid(self._session)
        result = await original(self, command)
        if result is None and not probe.first_holds_transaction.is_set():
            await probe.hold_first_transaction()
            if fault == "rollback":
                failed = True
                raise SQLAlchemyError("synthetic failure while competing receipt waits")
        return result

    monkeypatch.setattr(ShipmentReceipts, "claim", claim)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client, reference="FAIL-RACE", carrier_code="carrier-alpha", tracking_code="RACE-ONE"
        )

        async def post() -> httpx.Response:
            return await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="race-event",
                raw_body=_alpha_body("race-event", "RACE-ONE"),
            )

        responses = await run_with_proven_contention(postgres_database, probe, post, post)
        assert sorted(r.status_code for r in responses if isinstance(r, httpx.Response)) == [
            200,
            503,
        ]
        assert failed == (fault == "rollback")
        assert await _counts(postgres_database, postgres_tracking_database) == (1, 1, 1, 1)
        assert (await post()).json()["result"] == "DUPLICATE"
