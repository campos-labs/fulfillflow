"""Durable admission and actual command/result transport through both services."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from tests.async_flow import drain
from tests.service_pair import create_app


async def test_accepted_pending_duplicate_completion_and_bytes_conflict(
    postgres_database, postgres_tracking_database, postgres_settings, fixed_clock
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        shipment_id = await _create_shipment(
            client, reference="ASYNC-ONE", carrier_code="carrier-alpha", tracking_code="ASYNC-ONE"
        )
        body = _alpha_body("async-1", "ASYNC-ONE")
        response = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="async-1",
            raw_body=body,
        )
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert set(accepted) == {
            "inbox_event_id",
            "external_event_id",
            "status",
            "received_at",
            "request_id",
        }
        assert response.headers["retry-after"] == "1"
        pending = await client.get(response.headers["location"])
        assert pending.json()["progress"] == "QUEUED"
        assert pending.json()["completed_at"] is None
        duplicate = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="async-1",
            raw_body=body,
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["inbox_event_id"] == accepted["inbox_event_id"]
        async with postgres_tracking_database.session() as session:
            row = (
                await session.execute(text("SELECT raw_body, command FROM carrier_event_inbox"))
            ).one()
            assert row.raw_body == body
            assert row.command is not None
            assert await session.scalar(text("SELECT count(*) FROM message_outbox")) == 1
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        completed = (await client.get(response.headers["location"])).json()
        assert completed["status"] == "PROCESSED", completed
        assert completed["progress"] == "COMPLETED"
        assert completed["completed_at"] is not None
        assert completed["result"]["shipment_id"] == shipment_id
        duplicate = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="async-1",
            raw_body=body,
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["result"] == "DUPLICATE"
        conflict = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="async-1",
            raw_body=body + b" ",
        )
        assert conflict.status_code == 409
        async with postgres_database.session() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM message_outbox "
                        "WHERE type='shipment.status_changed.v1'"
                    )
                )
                == 1
            )
            assert await session.scalar(text("SELECT count(*) FROM tracking_event_receipts")) == 1
            assert await session.scalar(text("SELECT count(*) FROM message_outbox")) == 2
        async with postgres_tracking_database.session() as session:
            assert await session.scalar(text("SELECT count(*) FROM tracking_events")) == 1


async def test_permanent_rejection_after_acceptance_and_no_http_apply(
    postgres_database, postgres_tracking_database, postgres_settings, fixed_clock
):
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        body = _alpha_body("no-shipment", "MISSING")
        response = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="no-shipment",
            raw_body=body,
        )
        assert response.status_code == 202
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        result = (await client.get(response.headers["location"])).json()
        assert result["status"] == "REJECTED"
        assert result["result"]["code"] == "SHIPMENT_NOT_FOUND_FOR_TRACKING"
        assert result["completed_at"] is not None
        duplicate = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="no-shipment",
            raw_body=body,
        )
        assert duplicate.status_code == 422
        removed = await client.post(
            "/internal/v1/tracking-events",
            headers={
                "X-FulfillFlow-Internal-Token": (
                    postgres_settings.internal_api_secret.get_secret_value()
                )
            },
            json={},
        )
        assert removed.status_code == 404


async def test_tracking_finalization_failure_resumes_locally_without_repeating_core(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
    monkeypatch,
):
    from datetime import timedelta

    from fulfillflow.messaging.store import RetryableItemError
    from fulfillflow.tracking.repository import TrackingRepository

    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    original = TrackingRepository.add_tracking_event

    async def fail(self, value):
        await original(self, value)
        raise RetryableItemError("injected finalization failure")

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client, reference="FINALIZE", carrier_code="carrier-alpha", tracking_code="FINALIZE"
        )
        response = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="finalize",
            raw_body=_alpha_body("finalize", "FINALIZE"),
        )
        assert response.status_code == 202
        monkeypatch.setattr(TrackingRepository, "add_tracking_event", fail)
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        async with postgres_database.session() as session:
            receipt = (await session.execute(text("SELECT * FROM tracking_event_receipts"))).one()
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM message_outbox "
                        "WHERE type='shipment.status_changed.v1'"
                    )
                )
                == 1
            )
        async with postgres_tracking_database.session() as session:
            assert await session.scalar(text("SELECT count(*) FROM tracking_events")) == 0
            assert await session.scalar(text("SELECT state FROM message_inbox")) == "RETRY_WAIT"
            assert (
                await session.scalar(text("SELECT completed_at FROM carrier_event_inbox")) is None
            )
        assert (await client.get(response.headers["location"])).json()["status"] == "RECEIVED"
        monkeypatch.setattr(TrackingRepository, "add_tracking_event", original)
        fixed_clock.current += timedelta(seconds=2)
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        assert (await client.get(response.headers["location"])).json()["status"] == "PROCESSED"
        async with postgres_database.session() as session:
            assert (
                await session.execute(text("SELECT * FROM tracking_event_receipts"))
            ).one() == receipt
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM message_outbox "
                        "WHERE type='shipment.status_changed.v1'"
                    )
                )
                == 1
            )


@pytest.mark.parametrize(
    "field", ["command_sha256", "causation_id", "correlation_id", "request_id", "event_id"]
)
async def test_result_identity_mismatch_blocks_without_business_rejection(
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
    field,
):
    import hashlib
    from uuid import uuid4

    from fulfillflow.contracts.messages import (
        ResultEnvelope,
        ResultPayload,
        canonical_bytes,
        decode_message,
    )
    from fulfillflow.core.message_handler import apply_command
    from fulfillflow.messaging.store import process_one, put_message
    from fulfillflow.tracking.message_handler import apply_result
    from fulfillflow.tracking.message_tables import tables

    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        await _create_shipment(
            client, reference="BAD-RESULT", carrier_code="carrier-alpha", tracking_code="BAD-RESULT"
        )
        response = await _post_event(
            client,
            postgres_settings,
            fixed_clock,
            carrier_code="carrier-alpha",
            event_id="bad-result",
            raw_body=_alpha_body("bad-result", "BAD-RESULT"),
        )
        assert response.status_code == 202
        async with postgres_tracking_database.session() as session:
            command = decode_message(await session.scalar(text("SELECT body FROM message_outbox")))
            original = (
                await session.execute(text("SELECT command, raw_body FROM carrier_event_inbox"))
            ).one()
        async with postgres_database.session() as session, session.begin():
            await apply_command(session, command, fixed_clock)
        async with postgres_database.session() as session:
            valid = decode_message(
                await session.scalar(
                    text("SELECT body FROM message_outbox WHERE type='tracking.result.v1'")
                )
            )
        data = valid.model_dump(mode="json")
        if field == "command_sha256":
            data["payload"][field] = "0" * 64
        else:
            data[field] = str(uuid4())
            if field == "event_id":
                data["payload"]["result"]["event_id"] = data[field]
        data["payload_sha256"] = hashlib.sha256(
            canonical_bytes(ResultPayload.model_validate(data["payload"]))
        ).hexdigest()
        bad = ResultEnvelope.model_validate(data)

        async def apply(session, message):
            await apply_result(session, message, fixed_clock)

        async with postgres_tracking_database.session() as session, session.begin():
            await put_message(session, tables.inbox, bad, fixed_clock.now())
        async with postgres_tracking_database.session() as session, session.begin():
            assert await process_one(session, tables.inbox, fixed_clock.now(), apply)
        async with postgres_tracking_database.session() as session:
            assert await session.scalar(text("SELECT state FROM message_inbox")) == "BLOCKED"
            assert await session.scalar(text("SELECT count(*) FROM tracking_events")) == 0
            assert (
                await session.execute(text("SELECT command, raw_body FROM carrier_event_inbox"))
            ).one() == original
        detail = (await client.get(response.headers["location"])).json()
        assert detail["status"] == "RECEIVED"
        assert detail["completed_at"] is None
        assert detail["result"] is None
        if field != "correlation_id":
            assert detail["progress"] == "BLOCKED_LOCAL"
