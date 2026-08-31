"""Notifications REST contracts exercised through real Carrier webhooks."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from tests.support import FixedClock

from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.main import create_app
from fulfillflow.tracking.public import calculate_signature

pytestmark = pytest.mark.integration


async def _create_shipment(
    client: AsyncClient,
    *,
    reference: str,
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
    confirmation = await client.post(f"/api/v1/orders/{order_id}/confirm")
    assert confirmation.status_code == 200
    shipment_response = await client.post(
        "/api/v1/shipments",
        json={
            "order_id": order_id,
            "carrier_code": "carrier-alpha",
            "tracking_code": tracking_code,
        },
    )
    assert shipment_response.status_code == 201
    return str(shipment_response.json()["id"])


def _alpha_body(
    event_id: str,
    tracking_code: str,
    *,
    status: str,
    event_date: str,
) -> bytes:
    payload: dict[str, Any] = {
        "eventId": event_id,
        "trackingCode": tracking_code,
        "status": status,
        "eventDate": event_date,
        "city": "Carrier Payload City Sentinel",
        "description": "carrier-description-must-not-leak",
        "supplierExtension": {
            "apiToken": "supplier-api-token-must-not-leak",
            "signature": "supplier-signature-must-not-leak",
            "arbitrary": {"private": True},
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode()


async def _post_alpha_event(
    client: AsyncClient,
    settings: Settings,
    fixed_clock: FixedClock,
    *,
    event_id: str,
    raw_body: bytes,
) -> Response:
    timestamp = str(int(fixed_clock.current.timestamp()))
    signature = calculate_signature(
        settings.carrier_alpha_webhook_secret.get_secret_value(),
        timestamp=timestamp,
        event_id=event_id,
        raw_body=raw_body,
    )
    return await client.post(
        "/api/v1/carriers/carrier-alpha/events",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-FulfillFlow-Event-Id": event_id,
            "X-FulfillFlow-Timestamp": timestamp,
            "X-FulfillFlow-Signature": signature,
        },
    )


def _item_ids(response: Response) -> list[str]:
    assert response.status_code == 200
    return [item["id"] for item in response.json()["items"]]


async def test_notification_queries_cover_generation_filters_pagination_and_sanitization(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> None:
    initial_time = fixed_clock.current
    second_notification_time = initial_time + timedelta(hours=2)
    third_notification_time = initial_time + timedelta(hours=3)
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    openapi_paths = app.openapi()["paths"]
    assert "get" in openapi_paths["/api/v1/notifications"]
    assert "get" in openapi_paths["/api/v1/notifications/{notification_id}"]

    first_body = _alpha_body(
        "notification-event-001",
        "NOTIFICATION-FIRST",
        status="CREATED",
        event_date="2026-08-29T10:00:00Z",
    )
    no_change_body = _alpha_body(
        "notification-event-002",
        "NOTIFICATION-FIRST",
        status="CREATED",
        event_date="2026-08-29T10:10:00Z",
    )
    second_body = _alpha_body(
        "notification-event-003",
        "NOTIFICATION-FIRST",
        status="MOVING",
        event_date="2026-08-29T10:20:00Z",
    )
    third_body = _alpha_body(
        "notification-event-004",
        "NOTIFICATION-SECOND",
        status="CREATED",
        event_date="2026-08-29T10:30:00Z",
    )
    first_signature = calculate_signature(
        postgres_settings.carrier_alpha_webhook_secret.get_secret_value(),
        timestamp=str(int(initial_time.timestamp())),
        event_id="notification-event-001",
        raw_body=first_body,
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            first_shipment_id = await _create_shipment(
                client,
                reference="NOTIFICATION-FIRST",
                tracking_code="NOTIFICATION-FIRST",
            )
            second_shipment_id = await _create_shipment(
                client,
                reference="NOTIFICATION-SECOND",
                tracking_code="NOTIFICATION-SECOND",
            )

            first_applied = await _post_alpha_event(
                client,
                postgres_settings,
                fixed_clock,
                event_id="notification-event-001",
                raw_body=first_body,
            )
            fixed_clock.current = initial_time + timedelta(hours=1)
            no_state_change = await _post_alpha_event(
                client,
                postgres_settings,
                fixed_clock,
                event_id="notification-event-002",
                raw_body=no_change_body,
            )
            duplicate = await _post_alpha_event(
                client,
                postgres_settings,
                fixed_clock,
                event_id="notification-event-001",
                raw_body=first_body,
            )
            fixed_clock.current = second_notification_time
            second_applied = await _post_alpha_event(
                client,
                postgres_settings,
                fixed_clock,
                event_id="notification-event-003",
                raw_body=second_body,
            )
            fixed_clock.current = third_notification_time
            third_applied = await _post_alpha_event(
                client,
                postgres_settings,
                fixed_clock,
                event_id="notification-event-004",
                raw_body=third_body,
            )

            all_notifications = await client.get("/api/v1/notifications")
            repeated_list = await client.get("/api/v1/notifications")
            first_page = await client.get(
                "/api/v1/notifications",
                params={"page": 1, "page_size": 2},
            )
            second_page = await client.get(
                "/api/v1/notifications",
                params={"page": 2, "page_size": 2},
            )
            empty_page = await client.get(
                "/api/v1/notifications",
                params={"page": 3, "page_size": 2},
            )
            simulated = await client.get(
                "/api/v1/notifications",
                params={"status": "SIMULATED"},
            )
            failed = await client.get(
                "/api/v1/notifications",
                params={"status": "FAILED"},
            )
            first_shipment = await client.get(
                "/api/v1/notifications",
                params={"shipment_id": first_shipment_id},
            )
            second_shipment = await client.get(
                "/api/v1/notifications",
                params={"shipment_id": second_shipment_id},
            )
            created_from = await client.get(
                "/api/v1/notifications",
                params={"created_from": second_notification_time.isoformat()},
            )
            created_to = await client.get(
                "/api/v1/notifications",
                params={"created_to": second_notification_time.isoformat()},
            )

            notification_by_event = {
                item["tracking_event_id"]: item for item in all_notifications.json()["items"]
            }
            first_notification = notification_by_event[first_applied.json()["tracking_event_id"]]
            detail = await client.get(f"/api/v1/notifications/{first_notification['id']}")

    assert first_applied.status_code == 200
    assert first_applied.json()["result"] == "APPLIED"
    assert no_state_change.status_code == 200
    assert no_state_change.json()["result"] == "NO_STATE_CHANGE"
    assert duplicate.status_code == 200
    assert duplicate.json()["result"] == "DUPLICATE"
    assert duplicate.json()["tracking_event_id"] == first_applied.json()["tracking_event_id"]
    assert second_applied.status_code == 200
    assert second_applied.json()["result"] == "APPLIED"
    assert third_applied.status_code == 200
    assert third_applied.json()["result"] == "APPLIED"

    assert all_notifications.status_code == 200
    assert all_notifications.json()["total"] == 3
    assert all_notifications.json()["page"] == 1
    assert all_notifications.json()["page_size"] == 25
    expected_tracking_event_ids = [
        third_applied.json()["tracking_event_id"],
        second_applied.json()["tracking_event_id"],
        first_applied.json()["tracking_event_id"],
    ]
    assert [
        item["tracking_event_id"] for item in all_notifications.json()["items"]
    ] == expected_tracking_event_ids
    assert _item_ids(repeated_list) == _item_ids(all_notifications)
    assert _item_ids(first_page) + _item_ids(second_page) == _item_ids(all_notifications)
    assert empty_page.json() == {
        "items": [],
        "page": 3,
        "page_size": 2,
        "total": 3,
    }

    assert _item_ids(simulated) == _item_ids(all_notifications)
    assert failed.json() == {"items": [], "page": 1, "page_size": 25, "total": 0}
    assert {item["tracking_event_id"] for item in first_shipment.json()["items"]} == {
        first_applied.json()["tracking_event_id"],
        second_applied.json()["tracking_event_id"],
    }
    assert [item["tracking_event_id"] for item in second_shipment.json()["items"]] == [
        third_applied.json()["tracking_event_id"]
    ]
    assert [item["tracking_event_id"] for item in created_from.json()["items"]] == [
        third_applied.json()["tracking_event_id"],
        second_applied.json()["tracking_event_id"],
    ]
    assert [item["tracking_event_id"] for item in created_to.json()["items"]] == [
        second_applied.json()["tracking_event_id"],
        first_applied.json()["tracking_event_id"],
    ]

    allowed_keys = {
        "id",
        "shipment_id",
        "tracking_event_id",
        "channel",
        "recipient",
        "template_key",
        "message",
        "status",
        "error_detail",
        "created_at",
        "simulated_at",
    }
    for item in all_notifications.json()["items"]:
        assert set(item) == allowed_keys
        assert item["channel"] == "EMAIL"
        assert item["status"] == "SIMULATED"
        assert item["error_detail"] is None
        assert item["simulated_at"] == item["created_at"]

    assert detail.status_code == 200
    assert detail.json() == first_notification
    assert first_notification["shipment_id"] == first_shipment_id
    assert first_notification["recipient"] == "notification-first@example.test"
    assert first_notification["template_key"] == "shipment_posted"
    assert first_notification["message"] == "Your shipment has been posted."

    operational_text = "\n".join(
        response.text
        for response in (
            all_notifications,
            repeated_list,
            first_page,
            second_page,
            simulated,
            first_shipment,
            second_shipment,
            created_from,
            created_to,
            detail,
        )
    )
    for forbidden in (
        "raw_body",
        "payload_sha256",
        "parsed_payload",
        "supplierExtension",
        "supplier-api-token-must-not-leak",
        "supplier-signature-must-not-leak",
        "carrier-description-must-not-leak",
        "Carrier Payload City Sentinel",
        hashlib.sha256(first_body).hexdigest(),
        first_signature,
        postgres_settings.carrier_alpha_webhook_secret.get_secret_value(),
        postgres_settings.carrier_beta_webhook_secret.get_secret_value(),
    ):
        assert forbidden not in operational_text


async def test_notification_query_validation_and_missing_detail_use_problem_details(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> None:
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    request_id = "00000000-0000-4000-8000-000000000777"
    invalid_filters = [
        ({"status": "simulated"}, "status"),
        ({"shipment_id": "not-a-uuid"}, "shipment_id"),
        ({"created_from": "2026-08-29T12:00:00"}, "created_from"),
        ({"created_to": "not-a-datetime"}, "created_to"),
        ({"page": 0}, "page"),
        ({"page_size": 0}, "page_size"),
        ({"page_size": 101}, "page_size"),
    ]

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            validation_responses = [
                (
                    await client.get(
                        "/api/v1/notifications",
                        params=params,
                        headers={"X-Request-ID": request_id},
                    ),
                    field,
                )
                for params, field in invalid_filters
            ]
            invalid_identifier = await client.get(
                "/api/v1/notifications/not-a-uuid",
                headers={"X-Request-ID": request_id},
            )
            missing_identifier = "00000000-0000-4000-8000-000000000999"
            missing = await client.get(
                f"/api/v1/notifications/{missing_identifier}",
                headers={"X-Request-ID": request_id},
            )

    for response, field in validation_responses:
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.headers["X-Request-ID"] == request_id
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert body["request_id"] == request_id
        assert any(error["location"] == ["query", field] for error in body["errors"])

    assert invalid_identifier.status_code == 422
    assert invalid_identifier.headers["content-type"].startswith("application/problem+json")
    assert invalid_identifier.json()["code"] == "VALIDATION_ERROR"
    assert invalid_identifier.json()["errors"][0]["location"] == [
        "path",
        "notification_id",
    ]

    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.headers["X-Request-ID"] == request_id
    assert missing.json() == {
        "type": "https://fulfillflow.local/problems/resource-not-found",
        "title": "Resource not found",
        "status": 404,
        "code": "RESOURCE_NOT_FOUND",
        "detail": f"Notification {missing_identifier} was not found.",
        "request_id": request_id,
        "errors": [],
    }
