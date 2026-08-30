"""Orders and Shipments REST contract tests against real PostgreSQL."""

from datetime import date, timedelta
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from tests.support import FixedClock

from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.main import create_app
from fulfillflow.shipments.public import ShipmentService, ShipmentStatus

pytestmark = pytest.mark.integration


async def _create_order(client: AsyncClient, reference: str, *, confirm: bool = True) -> str:
    response = await client.post(
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
    assert response.status_code == 201
    order_id = response.json()["id"]
    if confirm:
        confirmation = await client.post(f"/api/v1/orders/{order_id}/confirm")
        assert confirmation.status_code == 200
    return order_id


async def _create_shipment(
    client: AsyncClient,
    order_id: str,
    tracking_code: str,
    *,
    carrier_code: str = "carrier-alpha",
) -> str:
    response = await client.post(
        "/api/v1/shipments",
        json={
            "order_id": order_id,
            "carrier_code": carrier_code,
            "tracking_code": tracking_code,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _insert_beta_carrier(database: Database, fixed_clock: FixedClock) -> None:
    async with database.session() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO carriers "
                "(id, code, name, adapter_key, active, created_at, updated_at) "
                "VALUES (:id, :code, :name, :adapter_key, true, :created_at, :updated_at)"
            ),
            {
                "id": UUID("00000000-0000-4000-8000-000000000101"),
                "code": "carrier-beta",
                "name": "Carrier Beta",
                "adapter_key": "beta",
                "created_at": fixed_clock.current,
                "updated_at": fixed_clock.current,
            },
        )


async def test_order_and_shipment_api_journey_filters_idempotency_and_conflicts(
    postgres_settings: Settings,
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
) -> None:
    del carrier_id
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    request_id = "00000000-0000-4000-8000-000000000777"
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            created_order = await client.post(
                "/api/v1/orders",
                headers={"X-Request-ID": request_id},
                json={
                    "external_reference": "ORDER-API-001",
                    "recipient": {
                        "name": "API Recipient",
                        "email": "api@example.test",
                        "postal_code": "09700-000",
                        "city": "Sao Bernardo do Campo",
                        "state": "sp",
                    },
                },
            )
            order_id = created_order.json()["id"]
            confirmed = await client.post(f"/api/v1/orders/{order_id}/confirm")
            repeated_confirmation = await client.post(f"/api/v1/orders/{order_id}/confirm")
            created_shipment = await client.post(
                "/api/v1/shipments",
                json={
                    "order_id": order_id,
                    "carrier_code": "CARRIER-ALPHA",
                    "tracking_code": " alpha-api-001 ",
                    "estimated_delivery_date": "2026-09-03",
                },
            )
            shipment_id = created_shipment.json()["id"]
            order_detail = await client.get(f"/api/v1/orders/{order_id}")
            shipment_detail = await client.get(f"/api/v1/shipments/{shipment_id}")
            shipment_page = await client.get(
                "/api/v1/shipments",
                params={
                    "status": "PENDING",
                    "carrier_code": "carrier-alpha",
                    "order_external_reference": "ORDER-API-001",
                    "tracking_code": "alpha-api-001",
                },
            )
            order_page = await client.get(
                "/api/v1/orders",
                params={"status": "CONFIRMED", "external_reference": "ORDER-API-001"},
            )
            cancelled_shipment = await client.post(f"/api/v1/shipments/{shipment_id}/cancel")
            repeated_cancellation = await client.post(f"/api/v1/shipments/{shipment_id}/cancel")
            invalid_order_cancel = await client.post(f"/api/v1/orders/{order_id}/cancel")
            duplicate_order = await client.post(
                "/api/v1/orders",
                json={
                    "external_reference": "ORDER-API-001",
                    "recipient": {
                        "name": "Duplicate",
                        "email": "duplicate@example.test",
                        "postal_code": "1",
                        "city": "City",
                        "state": "SP",
                    },
                },
            )

    assert created_order.status_code == 201
    assert created_order.headers["X-Request-ID"] == request_id
    assert created_order.json()["status"] == "CREATED"
    assert created_order.json()["recipient"]["state"] == "SP"
    assert confirmed.status_code == repeated_confirmation.status_code == 200
    assert confirmed.json()["status"] == repeated_confirmation.json()["status"] == "CONFIRMED"
    assert created_shipment.status_code == 201
    assert created_shipment.json()["status"] == "PENDING"
    assert created_shipment.json()["tracking_code"] == "ALPHA-API-001"
    assert created_shipment.json()["estimated_delivery_date"] == str(date(2026, 9, 3))
    assert order_detail.status_code == 200
    assert order_detail.json()["shipments"] == [
        {
            "id": shipment_id,
            "carrier_code": "carrier-alpha",
            "tracking_code": "ALPHA-API-001",
            "status": "PENDING",
            "estimated_delivery_date": "2026-09-03",
        }
    ]
    assert shipment_detail.json() == created_shipment.json()
    assert shipment_page.json()["total"] == 1
    assert shipment_page.json()["items"][0]["id"] == shipment_id
    assert order_page.json()["total"] == 1
    assert order_page.json()["items"][0]["id"] == order_id
    assert cancelled_shipment.json()["status"] == "CANCELLED"
    assert repeated_cancellation.json()["status"] == "CANCELLED"
    assert invalid_order_cancel.status_code == 409
    assert invalid_order_cancel.json()["code"] == "INVALID_ORDER_TRANSITION"
    assert duplicate_order.status_code == 409
    assert duplicate_order.json()["code"] == "RESOURCE_CONFLICT"


async def test_cancelled_order_cannot_be_confirmed_or_receive_shipment(
    postgres_settings: Settings,
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
) -> None:
    del carrier_id
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/v1/orders",
                json={
                    "external_reference": "ORDER-CANCEL-001",
                    "recipient": {
                        "name": "Cancelled",
                        "email": "cancelled@example.test",
                        "postal_code": "1",
                        "city": "City",
                        "state": "SP",
                    },
                },
            )
            order_id = created.json()["id"]
            cancelled = await client.post(f"/api/v1/orders/{order_id}/cancel")
            repeated = await client.post(f"/api/v1/orders/{order_id}/cancel")
            confirm = await client.post(f"/api/v1/orders/{order_id}/confirm")
            shipment = await client.post(
                "/api/v1/shipments",
                json={
                    "order_id": order_id,
                    "carrier_code": "carrier-alpha",
                    "tracking_code": "CANCELLED-ORDER",
                },
            )
            missing = await client.get("/api/v1/shipments/00000000-0000-4000-8000-000000000999")

    assert cancelled.status_code == repeated.status_code == 200
    assert cancelled.json()["status"] == repeated.json()["status"] == "CANCELLED"
    assert confirm.status_code == 409
    assert confirm.json()["code"] == "INVALID_ORDER_TRANSITION"
    assert shipment.status_code == 409
    assert shipment.json()["code"] == "INVALID_ORDER_TRANSITION"
    assert missing.status_code == 404
    assert missing.json()["code"] == "RESOURCE_NOT_FOUND"


async def test_each_order_and_shipment_filter_excludes_non_matching_records(
    postgres_settings: Settings,
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
) -> None:
    del carrier_id
    await _insert_beta_carrier(postgres_database, fixed_clock)
    initial_time = fixed_clock.current
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            first_order = await _create_order(client, "ORDER-FILTER-FIRST")
            first_shipment = await _create_shipment(client, first_order, "FILTER-FIRST")

            fixed_clock.current = initial_time + timedelta(hours=1)
            second_order = await _create_order(client, "ORDER-FILTER-SECOND")
            second_shipment = await _create_shipment(client, second_order, "FILTER-SECOND")
            event_time = fixed_clock.current + timedelta(minutes=5)
            async with postgres_database.session() as session:
                await ShipmentService(session, fixed_clock).apply_tracking_status(
                    UUID(second_shipment),
                    ShipmentStatus.POSTED,
                    occurred_at=event_time,
                    received_at=event_time + timedelta(minutes=1),
                    external_event_id="evt-filter-posted",
                )

            fixed_clock.current = initial_time + timedelta(hours=2)
            third_order = await _create_order(client, "ORDER-FILTER-THIRD")
            third_shipment = await _create_shipment(
                client,
                third_order,
                "FILTER-THIRD",
                carrier_code="carrier-beta",
            )

            fixed_clock.current = initial_time + timedelta(hours=3)
            fourth_order = await _create_order(client, "ORDER-FILTER-FOURTH")
            fourth_shipment = await _create_shipment(client, fourth_order, "FILTER-FOURTH")

            fixed_clock.current = initial_time + timedelta(hours=4)
            fifth_order = await _create_order(client, "ORDER-FILTER-CREATED", confirm=False)

            order_status = await client.get("/api/v1/orders", params={"status": "CREATED"})
            order_reference = await client.get(
                "/api/v1/orders",
                params={"external_reference": "ORDER-FILTER-SECOND"},
            )
            orders_from = await client.get(
                "/api/v1/orders",
                params={"created_from": (initial_time + timedelta(hours=2)).isoformat()},
            )
            orders_to = await client.get(
                "/api/v1/orders",
                params={"created_to": (initial_time + timedelta(hours=1)).isoformat()},
            )
            shipment_status = await client.get(
                "/api/v1/shipments",
                params={"status": "PENDING"},
            )
            shipment_carrier = await client.get(
                "/api/v1/shipments",
                params={"carrier_code": "carrier-beta"},
            )
            shipment_order = await client.get(
                "/api/v1/shipments",
                params={"order_external_reference": "ORDER-FILTER-SECOND"},
            )
            shipment_tracking = await client.get(
                "/api/v1/shipments",
                params={"tracking_code": "filter-fourth"},
            )
            shipments_from = await client.get(
                "/api/v1/shipments",
                params={"created_from": (initial_time + timedelta(hours=2)).isoformat()},
            )
            shipments_to = await client.get(
                "/api/v1/shipments",
                params={"created_to": (initial_time + timedelta(hours=1)).isoformat()},
            )

    def identifiers(response: Response) -> set[str]:
        return {item["id"] for item in response.json()["items"]}

    assert identifiers(order_status) == {fifth_order}
    assert identifiers(order_reference) == {second_order}
    assert identifiers(orders_from) == {third_order, fourth_order, fifth_order}
    assert identifiers(orders_to) == {first_order, second_order}
    assert identifiers(shipment_status) == {
        first_shipment,
        third_shipment,
        fourth_shipment,
    }
    assert identifiers(shipment_carrier) == {third_shipment}
    assert identifiers(shipment_order) == {second_shipment}
    assert identifiers(shipment_tracking) == {fourth_shipment}
    assert identifiers(shipments_from) == {third_shipment, fourth_shipment}
    assert identifiers(shipments_to) == {first_shipment, second_shipment}


async def test_pagination_is_stable_and_enforces_page_size_boundaries(
    postgres_settings: Settings,
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
) -> None:
    del carrier_id
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            order_ids = [await _create_order(client, f"ORDER-PAGE-{index}") for index in range(3)]
            shipment_ids = [
                await _create_shipment(client, order_id, f"SHIPMENT-PAGE-{index}")
                for index, order_id in enumerate(order_ids)
            ]

            order_first_page = await client.get(
                "/api/v1/orders", params={"page": 1, "page_size": 2}
            )
            order_second_page = await client.get(
                "/api/v1/orders", params={"page": 2, "page_size": 2}
            )
            order_empty_page = await client.get(
                "/api/v1/orders", params={"page": 3, "page_size": 2}
            )
            shipment_first_page = await client.get(
                "/api/v1/shipments", params={"page": 1, "page_size": 2}
            )
            shipment_second_page = await client.get(
                "/api/v1/shipments", params={"page": 2, "page_size": 2}
            )
            shipment_empty_page = await client.get(
                "/api/v1/shipments", params={"page": 3, "page_size": 2}
            )
            order_maximum = await client.get("/api/v1/orders", params={"page_size": 100})
            order_too_large = await client.get("/api/v1/orders", params={"page_size": 101})
            shipment_maximum = await client.get("/api/v1/shipments", params={"page_size": 100})
            shipment_too_small = await client.get("/api/v1/shipments", params={"page_size": 0})

    expected_orders = sorted(order_ids, reverse=True)
    expected_shipments = sorted(shipment_ids, reverse=True)
    assert [item["id"] for item in order_first_page.json()["items"]] == expected_orders[:2]
    assert [item["id"] for item in order_second_page.json()["items"]] == expected_orders[2:]
    assert order_empty_page.json() == {"items": [], "page": 3, "page_size": 2, "total": 3}
    assert [item["id"] for item in shipment_first_page.json()["items"]] == expected_shipments[:2]
    assert [item["id"] for item in shipment_second_page.json()["items"]] == expected_shipments[2:]
    assert shipment_empty_page.json() == {
        "items": [],
        "page": 3,
        "page_size": 2,
        "total": 3,
    }
    assert order_maximum.status_code == shipment_maximum.status_code == 200
    assert order_maximum.json()["page_size"] == shipment_maximum.json()["page_size"] == 100
    assert order_too_large.status_code == shipment_too_small.status_code == 422
    assert order_too_large.headers["content-type"].startswith("application/problem+json")
    assert shipment_too_small.headers["content-type"].startswith("application/problem+json")
