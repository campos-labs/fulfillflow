"""Carrier webhook and Tracking REST contracts against real PostgreSQL."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from tests.support import FixedClock

from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.main import create_app
from fulfillflow.tracking.public import calculate_signature
from fulfillflow.tracking.repository import TrackingRepository

pytestmark = pytest.mark.integration


async def _create_shipment(
    client: AsyncClient,
    *,
    reference: str,
    carrier_code: str,
    tracking_code: str,
) -> str:
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
    order_id = order_response.json()["id"]
    assert (await client.post(f"/api/v1/orders/{order_id}/confirm")).status_code == 200
    shipment_response = await client.post(
        "/api/v1/shipments",
        json={
            "order_id": order_id,
            "carrier_code": carrier_code,
            "tracking_code": tracking_code,
        },
    )
    assert shipment_response.status_code == 201
    return str(shipment_response.json()["id"])


def _secret(settings: Settings, carrier_code: str) -> str:
    if carrier_code == "carrier-alpha":
        return settings.carrier_alpha_webhook_secret.get_secret_value()
    return settings.carrier_beta_webhook_secret.get_secret_value()


async def _post_event(
    client: AsyncClient,
    settings: Settings,
    fixed_clock: FixedClock,
    *,
    carrier_code: str,
    event_id: str,
    raw_body: bytes,
    timestamp: str | None = None,
    secret: str | None = None,
    content_type: str = "application/json",
    request_id: str | None = None,
) -> Response:
    signed_timestamp = timestamp or str(int(fixed_clock.current.timestamp()))
    signature = calculate_signature(
        secret or _secret(settings, carrier_code),
        timestamp=signed_timestamp,
        event_id=event_id,
        raw_body=raw_body,
    )
    headers = {
        "Content-Type": content_type,
        "X-FulfillFlow-Event-Id": event_id,
        "X-FulfillFlow-Timestamp": signed_timestamp,
        "X-FulfillFlow-Signature": signature,
    }
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return await client.post(
        f"/api/v1/carriers/{carrier_code}/events",
        content=raw_body,
        headers=headers,
    )


def _alpha_body(
    event_id: str,
    tracking_code: str,
    *,
    status: str = "MOVING",
    event_date: str = "2026-08-29T11:30:00Z",
    description: str = "Transferência entre unidades",
) -> bytes:
    return (
        "{\n"
        f'  "eventId": {json.dumps(event_id)},\n'
        f'  "trackingCode": {json.dumps(tracking_code)},\n'
        f'  "status": {json.dumps(status)},\n'
        f'  "eventDate": {json.dumps(event_date)},\n'
        '  "city": "São Bernardo do Campo",\n'
        f'  "description": {json.dumps(description, ensure_ascii=False)},\n'
        '  "supplierExtension": {"keptOnlyInRawProjection": true, '
        '"apiToken": "supplier-sensitive-token", '
        '"signature": "supplier-extension-signature"}\n'
        "}\n"
    ).encode()


async def test_alpha_webhook_preserves_bytes_is_idempotent_and_exposes_timeline(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    openapi_paths = app.openapi()["paths"]
    assert "/api/v1/carrier-events/{inbox_event_id}" in openapi_paths
    assert "/api/v1/carrier-events/{event_id}" not in openapi_paths
    detail_parameters = openapi_paths["/api/v1/carrier-events/{inbox_event_id}"]["get"][
        "parameters"
    ]
    assert any(parameter["name"] == "inbox_event_id" for parameter in detail_parameters)
    request_id = "00000000-0000-4000-8000-000000000777"
    raw_body = _alpha_body("alpha-api-0001", " alpha-raw-0001 ")

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            shipment_id = await _create_shipment(
                client,
                reference="ORDER-TRACKING-ALPHA",
                carrier_code="carrier-alpha",
                tracking_code="ALPHA-RAW-0001",
            )
            applied = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="alpha-api-0001",
                raw_body=raw_body,
                request_id=request_id,
            )
            duplicate = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="alpha-api-0001",
                raw_body=raw_body,
            )
            conflict_body = _alpha_body(
                "alpha-api-0001",
                "ALPHA-RAW-0001",
                description="Different authenticated bytes",
            )
            conflict = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="alpha-api-0001",
                raw_body=conflict_body,
            )
            timeline = await client.get(f"/api/v1/shipments/{shipment_id}/tracking")
            inbox_page = await client.get(
                "/api/v1/carrier-events",
                params={
                    "carrier_code": "carrier-alpha",
                    "status": "PROCESSED",
                    "external_event_id": "alpha-api-0001",
                },
            )
            detail = await client.get(f"/api/v1/carrier-events/{applied.json()['inbox_event_id']}")
            async with postgres_database.session() as session:
                stored = (
                    await session.execute(
                        text(
                            "SELECT raw_body, parsed_payload FROM carrier_event_inbox "
                            "WHERE id = :id"
                        ),
                        {"id": UUID(applied.json()["inbox_event_id"])},
                    )
                ).one()
                counts = (
                    await session.execute(
                        text(
                            "SELECT "
                            "(SELECT count(*) FROM carrier_event_inbox), "
                            "(SELECT count(*) FROM tracking_events)"
                        )
                    )
                ).one()

    assert applied.status_code == 200
    assert applied.headers["X-Request-ID"] == request_id
    assert applied.json()["request_id"] == request_id
    assert applied.json()["result"] == "APPLIED"
    assert applied.json()["previous_status"] == "PENDING"
    assert applied.json()["current_status"] == "IN_TRANSIT"
    assert duplicate.status_code == 200
    assert duplicate.json()["result"] == "DUPLICATE"
    assert duplicate.json()["original_result"] == "APPLIED"
    assert duplicate.json()["inbox_event_id"] == applied.json()["inbox_event_id"]
    assert duplicate.json()["tracking_event_id"] == applied.json()["tracking_event_id"]
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "EVENT_ID_PAYLOAD_CONFLICT"
    assert counts == (1, 1)
    assert bytes(stored.raw_body) == raw_body
    assert stored.parsed_payload["city"] == "São Bernardo do Campo"
    assert stored.parsed_payload["supplierExtension"] == {
        "keptOnlyInRawProjection": True,
        "apiToken": "supplier-sensitive-token",
        "signature": "supplier-extension-signature",
    }
    assert timeline.status_code == 200
    assert timeline.json()["total"] == 1
    assert timeline.json()["items"][0]["external_status"] == "MOVING"
    assert timeline.json()["items"][0]["canonical_status"] == "IN_TRANSIT"
    assert inbox_page.status_code == 200
    assert inbox_page.json()["total"] == 1
    assert inbox_page.json()["items"][0]["tracking_event_id"] == applied.json()["tracking_event_id"]
    assert detail.status_code == 200
    assert detail.json()["payload"] == {
        "external_event_id": "alpha-api-0001",
        "tracking_code": "ALPHA-RAW-0001",
        "external_status": "MOVING",
        "occurred_at": "2026-08-29T11:30:00Z",
        "description": "Transferência entre unidades",
        "location": "São Bernardo do Campo",
    }
    operational_text = "\n".join(
        response.text for response in (applied, duplicate, timeline, inbox_page, detail)
    )
    signed_timestamp = str(int(fixed_clock.current.timestamp()))
    signed_signature = calculate_signature(
        postgres_settings.carrier_alpha_webhook_secret.get_secret_value(),
        timestamp=signed_timestamp,
        event_id="alpha-api-0001",
        raw_body=raw_body,
    )
    for forbidden in (
        "payload_sha256",
        "parsed_payload",
        "raw_body",
        "supplierExtension",
        "supplier-sensitive-token",
        "supplier-extension-signature",
        hashlib.sha256(raw_body).hexdigest(),
        signed_signature,
        postgres_settings.carrier_alpha_webhook_secret.get_secret_value(),
        postgres_settings.carrier_beta_webhook_secret.get_secret_value(),
    ):
        assert forbidden not in operational_text


async def test_beta_webhook_uses_its_own_schema_secret_and_location_projection(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    raw_payload: dict[str, Any] = {
        "id": "beta-api-0001",
        "tracking_number": " beta-raw-0001 ",
        "event": {
            "type": "hub_scan",
            "occurred_at": "2026-08-29T08:30:00-03:00",
            "details": "Recebido no centro de distribuição",
        },
        "location": {"city": "São Paulo", "state": "sp", "dock": "D-12"},
        "additional": "retained in parsed payload only",
    }
    raw_body = json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":")).encode()

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            shipment_id = await _create_shipment(
                client,
                reference="ORDER-TRACKING-BETA",
                carrier_code="carrier-beta",
                tracking_code="BETA-RAW-0001",
            )
            wrong_secret = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-beta",
                event_id="beta-api-0001",
                raw_body=raw_body,
                secret=postgres_settings.carrier_alpha_webhook_secret.get_secret_value(),
            )
            applied = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-beta",
                event_id="beta-api-0001",
                raw_body=raw_body,
            )
            timeline = await client.get(f"/api/v1/shipments/{shipment_id}/tracking")
            detail = await client.get(f"/api/v1/carrier-events/{applied.json()['inbox_event_id']}")

    assert wrong_secret.status_code == 401
    assert wrong_secret.json()["code"] == "INVALID_WEBHOOK_SIGNATURE"
    assert applied.status_code == 200
    assert applied.json()["result"] == "APPLIED"
    assert applied.json()["current_status"] == "IN_TRANSIT"
    assert timeline.json()["items"][0]["location"] == "São Paulo, SP"
    assert timeline.json()["items"][0]["description"] == ("Recebido no centro de distribuição")
    assert detail.status_code == 200
    assert detail.json()["payload"] == {
        "external_event_id": "beta-api-0001",
        "tracking_code": "BETA-RAW-0001",
        "external_status": "hub_scan",
        "occurred_at": "2026-08-29T08:30:00-03:00",
        "description": "Recebido no centro de distribuição",
        "location": "São Paulo, SP",
    }
    assert "additional" not in detail.text
    assert "dock" not in detail.text


async def test_permanent_payload_failures_reject_the_preserved_inbox(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    cases = [
        ("invalid-json", b'{"eventId":', "INVALID_JSON", None),
        (
            "unknown-status",
            _alpha_body("unknown-status", "REJECTION-TRACK", status="NOT_MAPPED"),
            "UNKNOWN_EXTERNAL_STATUS",
            "unknown-status",
        ),
        (
            "signed-id",
            _alpha_body("payload-id", "REJECTION-TRACK"),
            "EVENT_ID_MISMATCH",
            "payload-id",
        ),
        (
            "missing-tracking",
            _alpha_body("missing-tracking", "DOES-NOT-EXIST"),
            "SHIPMENT_NOT_FOUND_FOR_TRACKING",
            "missing-tracking",
        ),
        (
            "schema-invalid",
            json.dumps(
                {
                    "eventId": "schema-invalid",
                    "trackingCode": "REJECTION-TRACK",
                    "eventDate": "2026-08-29T11:30:00Z",
                }
            ).encode(),
            "VALIDATION_ERROR",
            "schema-invalid",
        ),
    ]

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            await _create_shipment(
                client,
                reference="ORDER-TRACKING-REJECTIONS",
                carrier_code="carrier-alpha",
                tracking_code="REJECTION-TRACK",
            )
            responses = [
                await _post_event(
                    client,
                    postgres_settings,
                    fixed_clock,
                    carrier_code="carrier-alpha",
                    event_id=event_id,
                    raw_body=raw_body,
                )
                for event_id, raw_body, _, _ in cases
            ]
            repeated = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="unknown-status",
                raw_body=cases[1][1],
            )
            async with postgres_database.session() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT external_event_id, raw_body, parsed_payload, status, "
                            "error_code FROM carrier_event_inbox ORDER BY external_event_id"
                        )
                    )
                ).all()
                tracking_count = await session.scalar(text("SELECT count(*) FROM tracking_events"))

    assert [response.status_code for response in responses] == [422] * len(cases)
    assert [response.json()["code"] for response in responses] == [case[2] for case in cases]
    assert repeated.status_code == 422
    assert repeated.json()["code"] == "UNKNOWN_EXTERNAL_STATUS"
    assert len(rows) == len(cases)
    by_id = {row.external_event_id: row for row in rows}
    for event_id, raw_body, error_code, parsed_id in cases:
        row = by_id[event_id]
        assert bytes(row.raw_body) == raw_body
        assert row.status == "REJECTED"
        assert row.error_code == error_code
        if parsed_id is None:
            assert row.parsed_payload is None
        else:
            assert row.parsed_payload["eventId"] == parsed_id
    assert tracking_count == 0


async def test_pre_authentication_failures_never_create_an_inbox(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    raw_body = _alpha_body("pre-auth", "PRE-AUTH")
    stale_timestamp = str(int((fixed_clock.current - timedelta(seconds=301)).timestamp()))

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            invalid_signature = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="pre-auth",
                raw_body=raw_body,
                secret="not-the-alpha-secret",
            )
            stale = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="pre-auth-stale",
                raw_body=raw_body,
                timestamp=stale_timestamp,
            )
            media_type = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="pre-auth-media",
                raw_body=raw_body,
                content_type="text/plain",
            )
            oversized_body = b"x" * 65_537
            oversized = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="pre-auth-oversized",
                raw_body=oversized_body,
            )
            unknown_carrier = await client.post(
                "/api/v1/carriers/not-configured/events",
                content=b"{}",
                headers={"Content-Type": "application/json"},
            )
            async with postgres_database.session() as session:
                inbox_count = await session.scalar(text("SELECT count(*) FROM carrier_event_inbox"))

    assert invalid_signature.status_code == 401
    assert invalid_signature.json()["code"] == "INVALID_WEBHOOK_SIGNATURE"
    assert stale.status_code == 401
    assert stale.json()["code"] == "STALE_WEBHOOK_TIMESTAMP"
    assert media_type.status_code == 415
    assert media_type.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert unknown_carrier.status_code == 404
    assert inbox_count == 0


async def test_signed_headers_require_one_visible_ascii_value_before_persistence(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    raw_body = _alpha_body("header-cardinality", "HEADER-CARDINALITY")
    timestamp = str(int(fixed_clock.current.timestamp()))
    signature = calculate_signature(
        postgres_settings.carrier_alpha_webhook_secret.get_secret_value(),
        timestamp=timestamp,
        event_id="header-cardinality",
        raw_body=raw_body,
    )
    base_headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"x-fulfillflow-event-id", b"header-cardinality"),
        (b"x-fulfillflow-timestamp", timestamp.encode("ascii")),
        (b"x-fulfillflow-signature", signature.encode("ascii")),
    ]
    signed_header_names = {
        b"x-fulfillflow-event-id",
        b"x-fulfillflow-timestamp",
        b"x-fulfillflow-signature",
    }

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            duplicates = []
            missing = []
            for name in signed_header_names:
                value = next(value for header, value in base_headers if header == name)
                duplicates.append(
                    await client.post(
                        "/api/v1/carriers/carrier-alpha/events",
                        content=raw_body,
                        headers=[*base_headers, (name, value)],
                    )
                )
                missing.append(
                    await client.post(
                        "/api/v1/carriers/carrier-alpha/events",
                        content=raw_body,
                        headers=[item for item in base_headers if item[0] != name],
                    )
                )

            invalid_event_ids = []
            for event_id in (
                "évent-non-ascii".encode(),
                b" leading-space",
                b"trailing-space ",
            ):
                invalid_event_ids.append(
                    await client.post(
                        "/api/v1/carriers/carrier-alpha/events",
                        content=raw_body,
                        headers=[
                            (name, event_id if name == b"x-fulfillflow-event-id" else value)
                            for name, value in base_headers
                        ],
                    )
                )

            async with postgres_database.session() as session:
                inbox_count = await session.scalar(text("SELECT count(*) FROM carrier_event_inbox"))

    responses = [*duplicates, *missing, *invalid_event_ids]
    assert all(response.status_code == 401 for response in responses)
    assert all(response.json()["code"] == "INVALID_WEBHOOK_SIGNATURE" for response in responses)
    assert inbox_count == 0


async def test_transaction_a_survives_failed_b_and_identical_delivery_resumes(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    raw_body = _alpha_body(
        "resume-after-rollback",
        "RESUME-AFTER-ROLLBACK",
        status="DELIVERED",
    )
    original_add = TrackingRepository.add_tracking_event

    async def fail_after_tracking_flush(
        repository: TrackingRepository,
        event: Any,
    ) -> None:
        await original_add(repository, event)
        raise SQLAlchemyError("injected failure after TrackingEvent flush")

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            shipment_id = await _create_shipment(
                client,
                reference="ORDER-TRACKING-RESUME",
                carrier_code="carrier-alpha",
                tracking_code="RESUME-AFTER-ROLLBACK",
            )
            monkeypatch.setattr(
                TrackingRepository,
                "add_tracking_event",
                fail_after_tracking_flush,
            )
            failed = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="resume-after-rollback",
                raw_body=raw_body,
            )

            async with postgres_database.session() as session:
                rolled_back = (
                    await session.execute(
                        text(
                            "SELECT i.status AS inbox_status, i.raw_body, "
                            "s.status AS shipment_status, o.status AS order_status, "
                            "(SELECT count(*) FROM carrier_event_inbox) AS inbox_count, "
                            "(SELECT count(*) FROM tracking_events) AS tracking_count "
                            "FROM carrier_event_inbox i "
                            "JOIN shipments s ON s.id = :shipment_id "
                            "JOIN orders o ON o.id = s.order_id "
                            "WHERE i.external_event_id = :event_id"
                        ),
                        {
                            "shipment_id": UUID(shipment_id),
                            "event_id": "resume-after-rollback",
                        },
                    )
                ).one()

            monkeypatch.setattr(
                TrackingRepository,
                "add_tracking_event",
                original_add,
            )
            resumed = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="resume-after-rollback",
                raw_body=raw_body,
            )
            async with postgres_database.session() as session:
                final = (
                    await session.execute(
                        text(
                            "SELECT i.status AS inbox_status, s.status AS shipment_status, "
                            "o.status AS order_status, "
                            "(SELECT count(*) FROM carrier_event_inbox) AS inbox_count, "
                            "(SELECT count(*) FROM tracking_events) AS tracking_count "
                            "FROM carrier_event_inbox i "
                            "JOIN shipments s ON s.id = :shipment_id "
                            "JOIN orders o ON o.id = s.order_id "
                            "WHERE i.external_event_id = :event_id"
                        ),
                        {
                            "shipment_id": UUID(shipment_id),
                            "event_id": "resume-after-rollback",
                        },
                    )
                ).one()

    assert failed.status_code == 503
    assert failed.json()["code"] == "DATABASE_UNAVAILABLE"
    assert rolled_back.inbox_count == 1
    assert rolled_back.tracking_count == 0
    assert rolled_back.inbox_status == "RECEIVED"
    assert bytes(rolled_back.raw_body) == raw_body
    assert rolled_back.shipment_status == "PENDING"
    assert rolled_back.order_status == "CONFIRMED"
    assert resumed.status_code == 200
    assert resumed.json()["result"] == "APPLIED"
    assert resumed.json()["current_status"] == "DELIVERED"
    assert final.inbox_count == 1
    assert final.tracking_count == 1
    assert final.inbox_status == "PROCESSED"
    assert final.shipment_status == "DELIVERED"
    assert final.order_status == "FULFILLED"


async def test_same_tracking_code_is_isolated_by_carrier(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    tracking_code = "SHARED-CARRIER-CODE"
    beta_body = json.dumps(
        {
            "id": "beta-shared-code",
            "tracking_number": tracking_code,
            "event": {
                "type": "completed",
                "occurred_at": "2026-08-29T11:31:00Z",
            },
        },
        separators=(",", ":"),
    ).encode()

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            alpha_shipment = await _create_shipment(
                client,
                reference="ORDER-SHARED-CODE-ALPHA",
                carrier_code="carrier-alpha",
                tracking_code=tracking_code,
            )
            beta_shipment = await _create_shipment(
                client,
                reference="ORDER-SHARED-CODE-BETA",
                carrier_code="carrier-beta",
                tracking_code=tracking_code,
            )
            alpha_response = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="alpha-shared-code",
                raw_body=_alpha_body("alpha-shared-code", tracking_code),
            )
            beta_response = await _post_event(
                client,
                postgres_settings,
                fixed_clock,
                carrier_code="carrier-beta",
                event_id="beta-shared-code",
                raw_body=beta_body,
            )
            alpha_read = await client.get(f"/api/v1/shipments/{alpha_shipment}")
            beta_read = await client.get(f"/api/v1/shipments/{beta_shipment}")

    assert alpha_response.status_code == beta_response.status_code == 200
    assert alpha_response.json()["shipment_id"] == alpha_shipment
    assert beta_response.json()["shipment_id"] == beta_shipment
    assert alpha_read.json()["status"] == "IN_TRANSIT"
    assert beta_read.json()["status"] == "DELIVERED"


async def test_timeline_records_no_change_stale_and_invalid_transition_without_regression(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    events = [
        ("state-001", "CREATED", "2026-08-29T10:00:00Z", "APPLIED"),
        ("state-002", "CREATED", "2026-08-29T10:10:00Z", "NO_STATE_CHANGE"),
        ("state-003", "MOVING", "2026-08-29T09:59:00Z", "IGNORED_STALE"),
        ("state-004", "DELIVERED", "2026-08-29T10:20:00Z", "APPLIED"),
        (
            "state-005",
            "MOVING",
            "2026-08-29T10:30:00Z",
            "IGNORED_INVALID_TRANSITION",
        ),
    ]

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            shipment_id = await _create_shipment(
                client,
                reference="ORDER-TRACKING-STATE-RESULTS",
                carrier_code="carrier-alpha",
                tracking_code="STATE-RESULTS",
            )
            responses = [
                await _post_event(
                    client,
                    postgres_settings,
                    fixed_clock,
                    carrier_code="carrier-alpha",
                    event_id=event_id,
                    raw_body=_alpha_body(
                        event_id,
                        "STATE-RESULTS",
                        status=external_status,
                        event_date=occurred_at,
                    ),
                )
                for event_id, external_status, occurred_at, _ in events
            ]
            shipment = await client.get(f"/api/v1/shipments/{shipment_id}")
            timeline = await client.get(
                f"/api/v1/shipments/{shipment_id}/tracking",
                params={"page": 1, "page_size": 3},
            )
            second_page = await client.get(
                f"/api/v1/shipments/{shipment_id}/tracking",
                params={"page": 2, "page_size": 3},
            )

    assert [response.status_code for response in responses] == [200] * len(events)
    assert [response.json()["result"] for response in responses] == [
        expected for *_, expected in events
    ]
    assert responses[2].json()["current_status"] == "POSTED"
    assert responses[4].json()["current_status"] == "DELIVERED"
    assert shipment.json()["status"] == "DELIVERED"
    assert timeline.json()["total"] == 5
    assert len(timeline.json()["items"]) == 3
    assert len(second_page.json()["items"]) == 2
    assert timeline.json()["items"][0]["application_result"] == ("IGNORED_INVALID_TRANSITION")
    timeline_by_id = {
        item["id"]: item for item in [*timeline.json()["items"], *second_page.json()["items"]]
    }
    expected_statuses = [
        ("APPLIED", "PENDING", "POSTED"),
        ("NO_STATE_CHANGE", "POSTED", "POSTED"),
        ("IGNORED_STALE", "POSTED", "POSTED"),
        ("APPLIED", "POSTED", "DELIVERED"),
        ("IGNORED_INVALID_TRANSITION", "DELIVERED", "DELIVERED"),
    ]
    for response, (result, previous, resulting) in zip(
        responses,
        expected_statuses,
        strict=True,
    ):
        event = timeline_by_id[response.json()["tracking_event_id"]]
        assert event["application_result"] == result
        assert event["previous_shipment_status"] == previous
        assert event["resulting_shipment_status"] == resulting
