"""Deterministic HTTP concurrency proofs for the synchronous Tracking pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, NoReturn, cast
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.distributed_state import event_state
from tests.service_pair import create_app
from tests.support import (
    ContentionProbe,
    FixedClock,
    _raise_after_concurrent_cleanup,
    run_with_proven_contention,
)

import fulfillflow.tracking.service as tracking_service_module
from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.orders.public import Order, OrderStatus
from fulfillflow.orders.repository import OrderRepository
from fulfillflow.shipments.public import Shipment
from fulfillflow.shipments.repository import ShipmentRepository
from fulfillflow.tracking.domain import CarrierEventInbox, TrackingEvent
from fulfillflow.tracking.public import calculate_signature
from fulfillflow.tracking.repository import TrackingRepository

pytestmark = pytest.mark.integration


async def _create_order_with_shipments(
    client: AsyncClient,
    *,
    reference: str,
    shipments: tuple[tuple[str, str], ...],
) -> tuple[UUID, list[UUID]]:
    order_response = await client.post(
        "/api/v1/orders",
        json={
            "external_reference": reference,
            "recipient": {
                "name": f"Recipient {reference}",
                "email": f"{reference.lower()}@example.test",
                "postal_code": "09700-000",
                "city": "Sao Bernardo do Campo",
                "state": "SP",
            },
        },
    )
    assert order_response.status_code == 201
    order_id = UUID(order_response.json()["id"])
    confirmation = await client.post(f"/api/v1/orders/{order_id}/confirm")
    assert confirmation.status_code == 200

    shipment_ids: list[UUID] = []
    for carrier_code, tracking_code in shipments:
        response = await client.post(
            "/api/v1/shipments",
            json={
                "order_id": str(order_id),
                "carrier_code": carrier_code,
                "tracking_code": tracking_code,
            },
        )
        assert response.status_code == 201
        shipment_ids.append(UUID(response.json()["id"]))
    return order_id, shipment_ids


def _alpha_body(
    event_id: str,
    tracking_code: str,
    *,
    status: str = "MOVING",
    event_date: str = "2026-08-29T11:30:00Z",
    description: str = "Concurrent Alpha event",
) -> bytes:
    return json.dumps(
        {
            "eventId": event_id,
            "trackingCode": tracking_code,
            "status": status,
            "eventDate": event_date,
            "city": "Sao Bernardo do Campo",
            "description": description,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _beta_body(
    event_id: str,
    tracking_code: str,
    *,
    event_type: str = "completed",
) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "tracking_number": tracking_code,
            "event": {
                "type": event_type,
                "occurred_at": "2026-08-29T11:30:00Z",
                "details": "Concurrent Beta event",
            },
            "location": {"city": "Sao Paulo", "state": "SP"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _carrier_secret(settings: Settings, carrier_code: str) -> str:
    if carrier_code == "carrier-alpha":
        return settings.carrier_alpha_webhook_secret.get_secret_value()
    return settings.carrier_beta_webhook_secret.get_secret_value()


async def _post_event(
    client: AsyncClient,
    settings: Settings,
    clock: FixedClock,
    *,
    carrier_code: str,
    event_id: str,
    raw_body: bytes,
) -> Response:
    timestamp = str(int(clock.current.timestamp()))
    return await client.post(
        f"/api/v1/carriers/{carrier_code}/events",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-FulfillFlow-Event-Id": event_id,
            "X-FulfillFlow-Timestamp": timestamp,
            "X-FulfillFlow-Signature": calculate_signature(
                _carrier_secret(settings, carrier_code),
                timestamp=timestamp,
                event_id=event_id,
                raw_body=raw_body,
            ),
        },
    )


def _responses(results: tuple[object, object]) -> tuple[Response, Response]:
    assert all(isinstance(result, Response) for result in results), results
    return cast(tuple[Response, Response], results)


def _assert_problem(response: Response, *, status_code: int, code: str) -> dict[str, Any]:
    assert response.status_code == status_code
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem["status"] == status_code
    assert problem["code"] == code
    assert problem["type"].endswith(code.lower().replace("_", "-"))
    assert problem["title"]
    assert problem["detail"]
    assert problem["errors"] == []
    UUID(problem["request_id"])
    return cast(dict[str, Any], problem)


def _hold_first_inbox_insert(
    monkeypatch: pytest.MonkeyPatch,
    probe: ContentionProbe,
) -> None:
    original_add = TrackingRepository.add_inbox

    async def paused_add(
        repository: TrackingRepository,
        inbox: CarrierEventInbox,
    ) -> None:
        await probe.record_backend_pid(repository._session)
        await original_add(repository, inbox)
        await probe.hold_first_transaction()

    monkeypatch.setattr(TrackingRepository, "add_inbox", paused_add)


def _hold_first_inbox_lock(
    monkeypatch: pytest.MonkeyPatch,
    probe: ContentionProbe,
) -> None:
    original_get = TrackingRepository.get_inbox

    async def paused_get(
        repository: TrackingRepository,
        inbox_event_id: UUID,
        *,
        for_update: bool = False,
    ) -> CarrierEventInbox | None:
        if for_update:
            await probe.record_backend_pid(repository._session)
        inbox = await original_get(repository, inbox_event_id, for_update=for_update)
        if for_update:
            await probe.hold_first_transaction()
        return inbox

    monkeypatch.setattr(TrackingRepository, "get_inbox", paused_get)


def _hold_first_shipment_lock(
    monkeypatch: pytest.MonkeyPatch,
    probe: ContentionProbe,
) -> None:
    original_get = ShipmentRepository.get_by_carrier_tracking_code

    async def paused_get(
        repository: ShipmentRepository,
        carrier_id: UUID,
        tracking_code: str,
        *,
        for_update: bool = False,
    ) -> Shipment | None:
        if for_update:
            await probe.record_backend_pid(repository._session)
        shipment = await original_get(
            repository,
            carrier_id,
            tracking_code,
            for_update=for_update,
        )
        if for_update:
            await probe.hold_first_transaction()
        return shipment

    monkeypatch.setattr(ShipmentRepository, "get_by_carrier_tracking_code", paused_get)


async def test_contention_cleanup_finishes_tasks_and_preserves_original_error() -> None:
    started = (asyncio.Event(), asyncio.Event())
    finished = (asyncio.Event(), asyncio.Event())
    never_release = asyncio.Event()

    async def participant(index: int) -> object:
        started[index].set()
        try:
            await never_release.wait()
        finally:
            finished[index].set()
        return object()

    tasks = (
        asyncio.create_task(participant(0), name="cleanup-first"),
        asyncio.create_task(participant(1), name="cleanup-second"),
    )
    await asyncio.wait_for(
        asyncio.gather(*(event.wait() for event in started)),
        timeout=1,
    )
    original_error = RuntimeError("original contention failure")

    with pytest.raises(RuntimeError) as raised:
        await _raise_after_concurrent_cleanup(
            tasks,
            original_error,
            cleanup_timeout_seconds=1,
        )

    assert raised.value is original_error
    assert all(event.is_set() for event in finished)
    assert all(task.done() for task in tasks)


async def test_contention_cleanup_exposes_timeout_with_original_error() -> None:
    started = asyncio.Event()
    first_cancellation = asyncio.Event()
    second_cancellation = asyncio.Event()
    never_release = asyncio.Event()

    async def cancellation_resistant_participant() -> object:
        started.set()
        try:
            await never_release.wait()
        except asyncio.CancelledError:
            first_cancellation.set()
            try:
                await never_release.wait()
            except asyncio.CancelledError:
                second_cancellation.set()
        return object()

    task = asyncio.create_task(
        cancellation_resistant_participant(),
        name="cleanup-cancellation-resistant",
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    original_error = RuntimeError("original contention failure")

    with pytest.raises(BaseExceptionGroup) as raised:
        await _raise_after_concurrent_cleanup(
            (task,),
            original_error,
            cleanup_timeout_seconds=0.01,
        )

    assert raised.value.exceptions[0] is original_error
    cleanup_error = raised.value.exceptions[1]
    assert isinstance(cleanup_error, TimeoutError)
    assert "cleanup-cancellation-resistant" in str(cleanup_error)
    assert first_cancellation.is_set()
    assert second_cancellation.is_set()
    assert task.done()


async def test_simultaneous_identical_delivery_is_idempotent_at_the_http_boundary(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    event_id = "concurrent-identical"
    tracking_code = "CONCURRENT-IDENTICAL"
    raw_body = _alpha_body(event_id, tracking_code, status="DELIVERED")

    async with app.router.lifespan_context(app):
        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://first.test",
            ) as first_client,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://second.test",
            ) as second_client,
        ):
            _order_id, shipment_ids = await _create_order_with_shipments(
                first_client,
                reference="ORDER-CONCURRENT-IDENTICAL",
                shipments=(("carrier-alpha", tracking_code),),
            )
            shipment_id = shipment_ids[0]
            probe = ContentionProbe()
            _hold_first_inbox_insert(monkeypatch, probe)

            first, second = _responses(
                await run_with_proven_contention(
                    postgres_database,
                    probe,
                    lambda: _post_event(
                        first_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=raw_body,
                    ),
                    lambda: _post_event(
                        second_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=raw_body,
                    ),
                )
            )
            state = await event_state(
                postgres_database,
                postgres_tracking_database,
                shipment_id=shipment_id,
                event_id=event_id,
            )

    assert {first.status_code, second.status_code} == {200}
    assert {first.json()["result"], second.json()["result"]} == {
        "APPLIED",
        "DUPLICATE",
    }
    duplicate = second if second.json()["result"] == "DUPLICATE" else first
    applied = first if first.json()["result"] == "APPLIED" else second
    assert duplicate.json()["original_result"] == "APPLIED"
    assert duplicate.json()["inbox_event_id"] == applied.json()["inbox_event_id"]
    assert duplicate.json()["tracking_event_id"] == applied.json()["tracking_event_id"]
    assert state.inbox_count == 1
    assert state.tracking_count == 1
    assert state.notification_count == 1
    assert state.inbox_status == "PROCESSED"
    assert bytes(state.raw_body) == raw_body
    assert state.payload_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert state.parsed_payload["description"] == "Concurrent Alpha event"
    assert state.shipment_status == "DELIVERED"
    assert state.order_status == "FULFILLED"


async def test_simultaneous_conflicting_payload_keeps_the_winning_forensic_record(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    event_id = "concurrent-payload-conflict"
    tracking_code = "CONCURRENT-PAYLOAD-CONFLICT"
    winning_body = _alpha_body(event_id, tracking_code, description="Winning bytes")
    losing_body = _alpha_body(event_id, tracking_code, description="Losing bytes")

    async with app.router.lifespan_context(app):
        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://winner.test",
            ) as winning_client,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://loser.test",
            ) as losing_client,
        ):
            _order_id, shipment_ids = await _create_order_with_shipments(
                winning_client,
                reference="ORDER-CONCURRENT-PAYLOAD-CONFLICT",
                shipments=(("carrier-alpha", tracking_code),),
            )
            shipment_id = shipment_ids[0]
            probe = ContentionProbe()
            _hold_first_inbox_insert(monkeypatch, probe)

            winner, loser = _responses(
                await run_with_proven_contention(
                    postgres_database,
                    probe,
                    lambda: _post_event(
                        winning_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=winning_body,
                    ),
                    lambda: _post_event(
                        losing_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=losing_body,
                    ),
                )
            )
            state = await event_state(
                postgres_database,
                postgres_tracking_database,
                shipment_id=shipment_id,
                event_id=event_id,
            )

    assert winner.status_code == 200
    assert winner.json()["result"] == "APPLIED"
    _assert_problem(loser, status_code=409, code="EVENT_ID_PAYLOAD_CONFLICT")
    assert state.inbox_count == 1
    assert state.tracking_count == 1
    assert state.inbox_status == "PROCESSED"
    assert bytes(state.raw_body) == winning_body
    assert state.payload_sha256 == hashlib.sha256(winning_body).hexdigest()
    assert state.payload_sha256 != hashlib.sha256(losing_body).hexdigest()
    assert state.parsed_payload["description"] == "Winning bytes"
    assert state.shipment_status == "IN_TRANSIT"
    assert state.order_status == "CONFIRMED"


async def test_two_concurrent_resumptions_finalize_received_inbox_without_reapplying_core(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    event_id = "concurrent-resume"
    tracking_code = "CONCURRENT-RESUME"
    raw_body = _alpha_body(event_id, tracking_code, status="DELIVERED")
    original_add_tracking = TrackingRepository.add_tracking_event

    async def fail_after_tracking_flush(
        repository: TrackingRepository,
        event: TrackingEvent,
    ) -> None:
        await original_add_tracking(repository, event)
        raise SQLAlchemyError("injected Transaction B failure")

    async with app.router.lifespan_context(app):
        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://first.test",
            ) as first_client,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://second.test",
            ) as second_client,
        ):
            _order_id, shipment_ids = await _create_order_with_shipments(
                first_client,
                reference="ORDER-CONCURRENT-RESUME",
                shipments=(("carrier-alpha", tracking_code),),
            )
            shipment_id = shipment_ids[0]
            monkeypatch.setattr(
                TrackingRepository,
                "add_tracking_event",
                fail_after_tracking_flush,
            )
            failed = await _post_event(
                first_client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id=event_id,
                raw_body=raw_body,
            )
            rolled_back = await event_state(
                postgres_database,
                postgres_tracking_database,
                shipment_id=shipment_id,
                event_id=event_id,
            )

            monkeypatch.setattr(
                TrackingRepository,
                "add_tracking_event",
                original_add_tracking,
            )
            probe = ContentionProbe()
            _hold_first_inbox_lock(monkeypatch, probe)
            first, second = _responses(
                await run_with_proven_contention(
                    postgres_database,
                    probe,
                    lambda: _post_event(
                        first_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=raw_body,
                    ),
                    lambda: _post_event(
                        second_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=raw_body,
                    ),
                )
            )
            final = await event_state(
                postgres_database,
                postgres_tracking_database,
                shipment_id=shipment_id,
                event_id=event_id,
            )

    _assert_problem(failed, status_code=503, code="DATABASE_UNAVAILABLE")
    assert rolled_back.inbox_status == "RECEIVED"
    assert bytes(rolled_back.raw_body) == raw_body
    assert rolled_back.payload_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert rolled_back.parsed_payload["eventId"] == event_id
    assert rolled_back.tracking_count == 0
    assert rolled_back.notification_count == rolled_back.receipt_count == 1
    assert rolled_back.shipment_status == "DELIVERED"
    assert rolled_back.order_status == "FULFILLED"
    assert first.status_code == second.status_code == 200
    assert first.json()["result"] == "APPLIED"
    assert second.json()["result"] == "DUPLICATE"
    assert second.json()["original_result"] == "APPLIED"
    assert second.json()["inbox_event_id"] == first.json()["inbox_event_id"]
    assert second.json()["tracking_event_id"] == first.json()["tracking_event_id"]
    assert final.inbox_count == 1
    assert final.tracking_count == 1
    assert final.notification_count == 1
    assert final.inbox_status == "PROCESSED"
    assert bytes(final.raw_body) == raw_body
    assert final.payload_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert final.parsed_payload["eventId"] == event_id
    assert final.shipment_status == "DELIVERED"
    assert final.order_status == "FULFILLED"


@pytest.mark.parametrize(
    (
        "first_event_id",
        "second_event_id",
        "first_status",
        "second_status",
        "first_occurred_at",
        "second_occurred_at",
        "second_clock_delta",
        "received_at_decides",
        "expected_first_result",
        "expected_second_result",
        "expected_winner_event_id",
        "expected_winner_occurred_at",
        "expected_winner_received_delta",
        "expected_shipment_status",
        "expected_order_status",
    ),
    [
        pytest.param(
            "serialized-event-a",
            "serialized-event-z",
            "MOVING",
            "MOVING",
            "2026-08-29T11:30:00Z",
            "2026-08-29T11:30:00Z",
            timedelta(0),
            False,
            "APPLIED",
            "NO_STATE_CHANGE",
            "serialized-event-z",
            "2026-08-29T11:30:00Z",
            timedelta(0),
            "IN_TRANSIT",
            "CONFIRMED",
            id="equal-times-larger-event-id-arrives-second",
        ),
        pytest.param(
            "serialized-event-a",
            "serialized-event-z",
            "MOVING",
            "DELIVERED",
            "2026-08-29T11:30:00Z",
            "2026-08-29T11:45:00Z",
            timedelta(minutes=1),
            False,
            "APPLIED",
            "APPLIED",
            "serialized-event-z",
            "2026-08-29T11:45:00Z",
            timedelta(minutes=1),
            "DELIVERED",
            "FULFILLED",
            id="older-locks-first-newer-wins-after-waiting",
        ),
        pytest.param(
            "serialized-event-z",
            "serialized-event-a",
            "DELIVERED",
            "MOVING",
            "2026-08-29T11:45:00Z",
            "2026-08-29T11:30:00Z",
            timedelta(minutes=1),
            False,
            "APPLIED",
            "IGNORED_STALE",
            "serialized-event-z",
            "2026-08-29T11:45:00Z",
            timedelta(0),
            "DELIVERED",
            "FULFILLED",
            id="newer-locks-first-older-is-stale-after-waiting",
        ),
        pytest.param(
            "serialized-event-z",
            "serialized-event-a",
            "MOVING",
            "DELIVERED",
            "2026-08-29T11:30:00Z",
            "2026-08-29T11:30:00Z",
            timedelta(minutes=1),
            True,
            "APPLIED",
            "APPLIED",
            "serialized-event-a",
            "2026-08-29T11:30:00Z",
            timedelta(minutes=1),
            "DELIVERED",
            "FULFILLED",
            id="equal-occurred-at-later-received-at-beats-larger-event-id",
        ),
    ],
)
async def test_distinct_events_for_one_shipment_serialize_by_total_event_key(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    first_event_id: str,
    second_event_id: str,
    first_status: str,
    second_status: str,
    first_occurred_at: str,
    second_occurred_at: str,
    second_clock_delta: timedelta,
    received_at_decides: bool,
    expected_first_result: str,
    expected_second_result: str,
    expected_winner_event_id: str,
    expected_winner_occurred_at: str,
    expected_winner_received_delta: timedelta,
    expected_shipment_status: str,
    expected_order_status: str,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    tracking_code = f"SERIALIZED-{expected_shipment_status}"
    first_body = _alpha_body(
        first_event_id,
        tracking_code,
        status=first_status,
        event_date=first_occurred_at,
    )
    second_body = _alpha_body(
        second_event_id,
        tracking_code,
        status=second_status,
        event_date=second_occurred_at,
    )
    initial_clock = fixed_clock.current

    async with app.router.lifespan_context(app):
        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://first.test",
            ) as first_client,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://second.test",
            ) as second_client,
        ):
            __order_id, shipment_ids = await _create_order_with_shipments(
                first_client,
                reference=f"ORDER-{tracking_code}",
                shipments=(("carrier-alpha", tracking_code),),
            )
            shipment_id = shipment_ids[0]
            probe = ContentionProbe()
            _hold_first_shipment_lock(monkeypatch, probe)

            async def post_later_arrival() -> Response:
                fixed_clock.current = initial_clock + second_clock_delta
                return await _post_event(
                    second_client,
                    postgres_settings,
                    fixed_clock,
                    carrier_code="carrier-alpha",
                    event_id=second_event_id,
                    raw_body=second_body,
                )

            first, second = _responses(
                await run_with_proven_contention(
                    postgres_database,
                    probe,
                    lambda: _post_event(
                        first_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=first_event_id,
                        raw_body=first_body,
                    ),
                    post_later_arrival,
                )
            )
            async with postgres_database.session() as session:
                shipment_state = (
                    await session.execute(
                        text(
                            "SELECT s.status, s.status_occurred_at, "
                            "s.status_event_received_at, s.status_external_event_id, "
                            "o.status AS order_status "
                            "FROM shipments s JOIN orders o ON o.id = s.order_id "
                            "WHERE s.id = :shipment_id"
                        ),
                        {"shipment_id": shipment_id},
                    )
                ).one()
            async with postgres_tracking_database.session() as session:
                events = (
                    await session.execute(
                        text(
                            "SELECT i.external_event_id, i.status AS inbox_status, "
                            "t.occurred_at, t.received_at, t.application_result, "
                            "t.resulting_shipment_status "
                            "FROM carrier_event_inbox i "
                            "JOIN tracking_events t ON t.inbox_event_id = i.id "
                            "WHERE i.external_event_id IN (:first, :second) "
                            "ORDER BY i.external_event_id"
                        ),
                        {"first": first_event_id, "second": second_event_id},
                    )
                ).all()
            async with postgres_database.session() as session:
                notification_count = await session.scalar(
                    text("SELECT count(*) FROM notifications WHERE shipment_id = :shipment_id"),
                    {"shipment_id": shipment_id},
                )

    assert first.status_code == second.status_code == 200
    assert first.json()["result"] == expected_first_result
    assert second.json()["result"] == expected_second_result
    assert second.json()["current_status"] == expected_shipment_status
    assert shipment_state.status == expected_shipment_status
    assert shipment_state.order_status == expected_order_status
    assert shipment_state.status_external_event_id == expected_winner_event_id
    assert shipment_state.status_occurred_at == datetime.fromisoformat(
        expected_winner_occurred_at.replace("Z", "+00:00")
    )
    assert shipment_state.status_event_received_at == (
        initial_clock + expected_winner_received_delta
    )
    assert len(events) == 2
    assert notification_count == sum(
        result == "APPLIED" for result in (expected_first_result, expected_second_result)
    )
    by_event_id = {row.external_event_id: row for row in events}
    assert by_event_id[first_event_id].inbox_status == "PROCESSED"
    assert by_event_id[first_event_id].application_result == expected_first_result
    assert by_event_id[second_event_id].inbox_status == "PROCESSED"
    assert by_event_id[second_event_id].application_result == expected_second_result
    assert by_event_id[second_event_id].resulting_shipment_status == expected_shipment_status
    assert by_event_id[first_event_id].received_at == initial_clock
    assert by_event_id[second_event_id].received_at == initial_clock + second_clock_delta
    if received_at_decides:
        assert by_event_id[first_event_id].occurred_at == by_event_id[second_event_id].occurred_at
        assert by_event_id[first_event_id].received_at < by_event_id[second_event_id].received_at
        assert first_event_id > second_event_id
        assert expected_winner_event_id == second_event_id
        assert expected_second_result == "APPLIED"


async def test_final_deliveries_follow_core_lock_order_and_keep_inbox_locks_local(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    first_event_id = "final-delivery-a"
    second_event_id = "final-delivery-b"
    lock_steps: dict[int, list[str]] = defaultdict(list)
    fulfillment_writes: list[UUID] = []

    async with app.router.lifespan_context(app):
        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://first.test",
            ) as first_client,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://second.test",
            ) as second_client,
        ):
            order_id, shipment_ids = await _create_order_with_shipments(
                first_client,
                reference="ORDER-CONCURRENT-FINAL-DELIVERIES",
                shipments=(
                    ("carrier-alpha", "FINAL-DELIVERY-A"),
                    ("carrier-alpha", "FINAL-DELIVERY-B"),
                ),
            )
            first_shipment_id, second_shipment_id = shipment_ids
            probe = ContentionProbe()
            original_inbox_get = TrackingRepository.get_inbox
            original_shipment_get = ShipmentRepository.get_by_carrier_tracking_code
            original_order_get = OrderRepository.get
            original_order_save = OrderRepository.save

            async def backend_pid(session: AsyncSession) -> int:
                pid = await session.scalar(text("SELECT pg_backend_pid()"))
                assert pid is not None
                return pid

            async def observed_inbox_get(
                repository: TrackingRepository,
                inbox_event_id: UUID,
                *,
                for_update: bool = False,
            ) -> CarrierEventInbox | None:
                inbox = await original_inbox_get(
                    repository,
                    inbox_event_id,
                    for_update=for_update,
                )
                if for_update:
                    lock_steps[await backend_pid(repository._session)].append("inbox")
                return inbox

            async def observed_shipment_get(
                repository: ShipmentRepository,
                carrier_id: UUID,
                tracking_code: str,
                *,
                for_update: bool = False,
            ) -> Shipment | None:
                shipment = await original_shipment_get(
                    repository,
                    carrier_id,
                    tracking_code,
                    for_update=for_update,
                )
                if for_update:
                    lock_steps[await backend_pid(repository._session)].append("shipment")
                return shipment

            async def paused_order_get(
                repository: OrderRepository,
                locked_order_id: UUID,
                *,
                for_update: bool = False,
                for_share: bool = False,
            ) -> Order | None:
                pid: int | None = None
                if for_update:
                    await probe.record_backend_pid(repository._session)
                    pid = await backend_pid(repository._session)
                    lock_steps[pid].append("order-attempt")
                order = await original_order_get(
                    repository,
                    locked_order_id,
                    for_update=for_update,
                    for_share=for_share,
                )
                if for_update:
                    assert pid is not None
                    lock_steps[pid].append("order")
                    await probe.hold_first_transaction()
                return order

            async def observed_order_save(
                repository: OrderRepository,
                order: Order,
            ) -> None:
                if order.status is OrderStatus.FULFILLED:
                    fulfillment_writes.append(order.id)
                await original_order_save(repository, order)

            monkeypatch.setattr(TrackingRepository, "get_inbox", observed_inbox_get)
            monkeypatch.setattr(
                ShipmentRepository,
                "get_by_carrier_tracking_code",
                observed_shipment_get,
            )
            monkeypatch.setattr(OrderRepository, "get", paused_order_get)
            monkeypatch.setattr(OrderRepository, "save", observed_order_save)

            first, second = _responses(
                await run_with_proven_contention(
                    postgres_database,
                    probe,
                    lambda: _post_event(
                        first_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=first_event_id,
                        raw_body=_alpha_body(
                            first_event_id,
                            "FINAL-DELIVERY-A",
                            status="DELIVERED",
                        ),
                    ),
                    lambda: _post_event(
                        second_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=second_event_id,
                        raw_body=_alpha_body(
                            second_event_id,
                            "FINAL-DELIVERY-B",
                            status="DELIVERED",
                        ),
                    ),
                )
            )
            async with postgres_database.session() as session:
                order_status = await session.scalar(
                    text("SELECT status FROM orders WHERE id = :order_id"),
                    {"order_id": order_id},
                )
                shipment_statuses = (
                    await session.execute(
                        text(
                            "SELECT id, status FROM shipments "
                            "WHERE id IN (:first_id, :second_id) ORDER BY id"
                        ),
                        {
                            "first_id": first_shipment_id,
                            "second_id": second_shipment_id,
                        },
                    )
                ).all()
            async with postgres_tracking_database.session() as session:
                inbox_events = (
                    await session.execute(
                        text(
                            "SELECT i.external_event_id, i.status, count(t.id) AS event_count "
                            "FROM carrier_event_inbox i "
                            "LEFT JOIN tracking_events t ON t.inbox_event_id = i.id "
                            "WHERE i.external_event_id IN (:first_event, :second_event) "
                            "GROUP BY i.id, i.external_event_id, i.status "
                            "ORDER BY i.external_event_id"
                        ),
                        {
                            "first_event": first_event_id,
                            "second_event": second_event_id,
                        },
                    )
                ).all()

    assert first.status_code == second.status_code == 200
    assert first.json()["result"] == second.json()["result"] == "APPLIED"
    assert first.json()["current_status"] == second.json()["current_status"] == "DELIVERED"
    first_pid = probe.first_pid.result()
    second_pid = probe.second_pid.result()
    assert first_pid != second_pid
    assert lock_steps[first_pid] == ["shipment", "order-attempt", "order"]
    assert lock_steps[second_pid] == ["shipment", "order-attempt", "order"]
    tracking_steps = [
        steps for pid, steps in lock_steps.items() if pid not in {first_pid, second_pid}
    ]
    assert sum(len(steps) for steps in tracking_steps) == 4
    assert all(set(steps) == {"inbox"} for steps in tracking_steps)
    assert fulfillment_writes == [order_id]
    assert order_status == "FULFILLED"
    assert len(shipment_statuses) == 2
    assert {row.status for row in shipment_statuses} == {"DELIVERED"}
    assert len(inbox_events) == 2
    assert all(row.status == "PROCESSED" for row in inbox_events)
    assert all(row.event_count == 1 for row in inbox_events)


async def test_concurrent_repetition_of_permanent_rejection_is_stable(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    event_id = "concurrent-rejection"
    tracking_code = "CONCURRENT-REJECTION"
    raw_body = _alpha_body(event_id, tracking_code, status="NOT-A-CARRIER-STATUS")

    async with app.router.lifespan_context(app):
        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://first.test",
            ) as first_client,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://second.test",
            ) as second_client,
        ):
            _order_id, shipment_ids = await _create_order_with_shipments(
                first_client,
                reference="ORDER-CONCURRENT-REJECTION",
                shipments=(("carrier-alpha", tracking_code),),
            )
            shipment_id = shipment_ids[0]
            probe = ContentionProbe()
            _hold_first_inbox_insert(monkeypatch, probe)

            first, second = _responses(
                await run_with_proven_contention(
                    postgres_database,
                    probe,
                    lambda: _post_event(
                        first_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=raw_body,
                    ),
                    lambda: _post_event(
                        second_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=raw_body,
                    ),
                )
            )
            state = await event_state(
                postgres_database,
                postgres_tracking_database,
                shipment_id=shipment_id,
                event_id=event_id,
            )

    first_problem = _assert_problem(
        first,
        status_code=422,
        code="UNKNOWN_EXTERNAL_STATUS",
    )
    second_problem = _assert_problem(
        second,
        status_code=422,
        code="UNKNOWN_EXTERNAL_STATUS",
    )
    for field in ("type", "title", "status", "code", "detail", "errors"):
        assert first_problem[field] == second_problem[field]
    assert state.inbox_count == 1
    assert state.tracking_count == 0
    assert state.inbox_status == "REJECTED"
    assert state.error_code == "UNKNOWN_EXTERNAL_STATUS"
    assert state.error_detail == first_problem["detail"]
    assert bytes(state.raw_body) == raw_body
    assert state.payload_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert state.parsed_payload["status"] == "NOT-A-CARRIER-STATUS"
    assert state.shipment_status == "PENDING"
    assert state.order_status == "CONFIRMED"


async def test_committed_rejection_short_circuits_concurrent_repetitions(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    event_id = "committed-rejection-short-circuit"
    tracking_code = "COMMITTED-REJECTION-SHORT-CIRCUIT"
    raw_body = _alpha_body(event_id, tracking_code, status="NOT-A-CARRIER-STATUS")
    normalization_calls = 0

    async with app.router.lifespan_context(app):
        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://first.test",
            ) as first_client,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://second.test",
            ) as second_client,
        ):
            _order_id, shipment_ids = await _create_order_with_shipments(
                first_client,
                reference="ORDER-COMMITTED-REJECTION-SHORT-CIRCUIT",
                shipments=(("carrier-alpha", tracking_code),),
            )
            shipment_id = shipment_ids[0]

            async def rejection_state() -> dict[str, Any]:
                row = await event_state(
                    postgres_database,
                    postgres_tracking_database,
                    shipment_id=shipment_id,
                    event_id=event_id,
                )
                return dict(row._mapping)

            initial_response = await _post_event(
                first_client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id=event_id,
                raw_body=raw_body,
            )
            initial_problem = _assert_problem(
                initial_response,
                status_code=422,
                code="UNKNOWN_EXTERNAL_STATUS",
            )
            initial_state = await rejection_state()
            assert initial_state["inbox_status"] == "REJECTED"
            assert initial_state["processed_at"] is not None

            def normalization_must_not_run(
                adapter_key: str,
                payload: Any,
            ) -> NoReturn:
                nonlocal normalization_calls
                del adapter_key, payload
                normalization_calls += 1
                raise AssertionError("normalization ran for a committed REJECTED inbox")

            monkeypatch.setattr(
                tracking_service_module,
                "normalize_carrier_event",
                normalization_must_not_run,
            )
            probe = ContentionProbe()
            _hold_first_inbox_lock(monkeypatch, probe)

            first, second = _responses(
                await run_with_proven_contention(
                    postgres_database,
                    probe,
                    lambda: _post_event(
                        first_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=raw_body,
                    ),
                    lambda: _post_event(
                        second_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=raw_body,
                    ),
                )
            )
            final_state = await rejection_state()

    first_problem = _assert_problem(
        first,
        status_code=422,
        code="UNKNOWN_EXTERNAL_STATUS",
    )
    second_problem = _assert_problem(
        second,
        status_code=422,
        code="UNKNOWN_EXTERNAL_STATUS",
    )
    for problem in (first_problem, second_problem):
        for field in ("type", "title", "status", "code", "detail", "errors"):
            assert problem[field] == initial_problem[field]
    assert normalization_calls == 0
    assert final_state == initial_state
    assert final_state["inbox_count"] == 1
    assert final_state["tracking_count"] == 0
    assert bytes(final_state["raw_body"]) == raw_body
    assert final_state["payload_sha256"] == hashlib.sha256(raw_body).hexdigest()
    assert final_state["parsed_payload"]["status"] == "NOT-A-CARRIER-STATUS"
    assert final_state["error_code"] == "UNKNOWN_EXTERNAL_STATUS"
    assert final_state["error_detail"] == initial_problem["detail"]
    assert final_state["shipment_status"] == "PENDING"
    assert final_state["status_event_received_at"] is None
    assert final_state["status_external_event_id"] is None
    assert final_state["shipped_at"] is None
    assert final_state["delivered_at"] is None
    assert final_state["order_status"] == "CONFIRMED"


async def test_same_external_event_id_is_concurrently_isolated_between_carriers(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    event_id = "carrier-isolated-concurrent-event"
    tracking_code = "CARRIER-ISOLATED-CONCURRENT"
    barrier = asyncio.Barrier(2)
    backend_pids: list[int] = []
    original_add = TrackingRepository.add_inbox

    async def synchronized_add(
        repository: TrackingRepository,
        inbox: CarrierEventInbox,
    ) -> None:
        pid = await repository._session.scalar(text("SELECT pg_backend_pid()"))
        assert pid is not None
        backend_pids.append(pid)
        await barrier.wait()
        await original_add(repository, inbox)

    async with app.router.lifespan_context(app):
        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://alpha.test",
            ) as alpha_client,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://beta.test",
            ) as beta_client,
        ):
            alpha_order_id, alpha_shipments = await _create_order_with_shipments(
                alpha_client,
                reference="ORDER-CARRIER-ISOLATION-ALPHA",
                shipments=(("carrier-alpha", tracking_code),),
            )
            beta_order_id, beta_shipments = await _create_order_with_shipments(
                beta_client,
                reference="ORDER-CARRIER-ISOLATION-BETA",
                shipments=(("carrier-beta", tracking_code),),
            )
            monkeypatch.setattr(TrackingRepository, "add_inbox", synchronized_add)
            alpha, beta = await asyncio.wait_for(
                asyncio.gather(
                    _post_event(
                        alpha_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-alpha",
                        event_id=event_id,
                        raw_body=_alpha_body(event_id, tracking_code),
                    ),
                    _post_event(
                        beta_client,
                        postgres_settings,
                        fixed_clock,
                        carrier_code="carrier-beta",
                        event_id=event_id,
                        raw_body=_beta_body(event_id, tracking_code),
                    ),
                ),
                timeout=10,
            )
            async with postgres_tracking_database.session() as session:
                inboxes = (
                    await session.execute(
                        text(
                            "SELECT i.carrier_id, i.status, count(t.id) AS event_count "
                            "FROM carrier_event_inbox i "
                            "LEFT JOIN tracking_events t ON t.inbox_event_id = i.id "
                            "WHERE i.external_event_id = :event_id "
                            "GROUP BY i.id, i.carrier_id, i.status"
                        ),
                        {"event_id": event_id},
                    )
                ).all()
            async with postgres_database.session() as session:
                shipment_states = (
                    await session.execute(
                        text("SELECT id, status FROM shipments WHERE id IN (:alpha_id, :beta_id)"),
                        {
                            "alpha_id": alpha_shipments[0],
                            "beta_id": beta_shipments[0],
                        },
                    )
                ).all()
                order_states = (
                    await session.execute(
                        text("SELECT id, status FROM orders WHERE id IN (:alpha_id, :beta_id)"),
                        {"alpha_id": alpha_order_id, "beta_id": beta_order_id},
                    )
                ).all()

    assert alpha.status_code == beta.status_code == 200
    assert alpha.json()["result"] == beta.json()["result"] == "APPLIED"
    assert len(backend_pids) == 2
    assert len(set(backend_pids)) == 2
    assert len(inboxes) == 2
    assert len({row.carrier_id for row in inboxes}) == 2
    assert all(row.status == "PROCESSED" for row in inboxes)
    assert all(row.event_count == 1 for row in inboxes)
    shipment_by_id = {row.id: row.status for row in shipment_states}
    assert shipment_by_id[alpha_shipments[0]] == "IN_TRANSIT"
    assert shipment_by_id[beta_shipments[0]] == "DELIVERED"
    order_by_id = {row.id: row.status for row in order_states}
    assert order_by_id[alpha_order_id] == "CONFIRMED"
    assert order_by_id[beta_order_id] == "FULFILLED"
