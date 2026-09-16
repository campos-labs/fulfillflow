"""Operational data, sanitization, results and Notification presentation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from httpx import AsyncClient

from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.notifications.public import (
    ExpectedNotificationFailure,
    NotificationsPublic,
)
from tests.async_flow import drain
from tests.support import FixedClock
from tests.ui.support import (
    confirm_order,
    create_order,
    create_shipment,
    send_alpha_event,
)


async def test_real_data_renders_all_tracking_results_and_sanitizes_external_content(
    ui_client: AsyncClient,
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_database: Database,
    fixed_clock: FixedClock,
) -> None:
    malicious = '<img src=x onerror="alert(1)">'
    raw_only = '<script id="raw-only">secret raw extension</script>'
    order = await create_order(
        ui_client,
        reference="UI-ESCAPE-0001",
        name="<script>alert('recipient')</script>",
    )
    await confirm_order(ui_client, order["id"])
    shipment = await create_shipment(ui_client, order["id"], tracking_code="UIESCAPE0001")

    base = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    cases = (
        ("ui-result-applied", "CREATED", base, "APPLIED", malicious),
        ("ui-result-same", "CREATED", base + timedelta(minutes=1), "NO_STATE_CHANGE", "same"),
        ("ui-result-stale", "MOVING", base - timedelta(minutes=1), "IGNORED_STALE", "stale"),
        ("ui-result-delivered", "DELIVERED", base + timedelta(minutes=2), "APPLIED", "delivered"),
        (
            "ui-result-invalid",
            "MOVING",
            base + timedelta(minutes=3),
            "IGNORED_INVALID_TRANSITION",
            "invalid",
        ),
    )
    outcomes: list[dict[str, object]] = []
    for event_id, status, occurred_at, expected, description in cases:
        response = await send_alpha_event(
            ui_client,
            postgres_settings,
            event_id=event_id,
            tracking_code=shipment["tracking_code"],
            external_status=status,
            occurred_at=occurred_at,
            description=description,
            extra={"unboundedExternalField": raw_only},
        )
        assert response.status_code == 202, response.text
        pending = await ui_client.get(f"/carrier-events/{response.json()['inbox_event_id']}")
        assert 'hx-trigger="every 1s"' in pending.text
        await drain(postgres_database, postgres_tracking_database, fixed_clock)
        detail = (await ui_client.get(response.headers["location"])).json()
        assert detail["result"]["result"] == expected
        outcomes.append(dict(detail, inbox_event_id=detail["id"]))

    def fail_renderer(resulting_status: str) -> None:
        del resulting_status
        raise ExpectedNotificationFailure

    async with postgres_database.session() as session:
        async with session.begin():
            failed = await NotificationsPublic(
                session,
                fixed_clock,
                renderer=fail_renderer,
            ).record_applied_transition(
                shipment_id=UUID(shipment["id"]),
                tracking_event_id=UUID(str(outcomes[1]["tracking_event_id"])),
                recipient="ui-recipient@example.test",
                resulting_status="POSTED",
            )

    order_page = await ui_client.get(f"/orders/{order['id']}")
    shipment_page = await ui_client.get(f"/shipments/{shipment['id']}")
    timeline = await ui_client.get(f"/shipments/{shipment['id']}/tracking")
    inbox = await ui_client.get("/carrier-events")
    inbox_detail = await ui_client.get(f"/carrier-events/{outcomes[0]['inbox_event_id']}")
    notifications = await ui_client.get("/notifications")
    simulated_api = await ui_client.get("/api/v1/notifications?status=SIMULATED")
    simulated_id = simulated_api.json()["items"][0]["id"]
    simulated_detail = await ui_client.get(f"/notifications/{simulated_id}")
    failed_detail = await ui_client.get(f"/notifications/{failed.id}")

    assert "FULFILLED" in order_page.text
    assert "DELIVERED" in shipment_page.text
    for result in (
        "APPLIED",
        "NO_STATE_CHANGE",
        "IGNORED_STALE",
        "IGNORED_INVALID_TRANSITION",
    ):
        assert result in timeline.text
    assert malicious not in timeline.text
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in timeline.text
    assert "<script>alert('recipient')</script>" not in order_page.text
    assert "&lt;script&gt;alert" in order_page.text

    assert "ui-result-applied" in inbox.text
    assert "UIESCAPE0001" in inbox_detail.text
    assert raw_only not in inbox_detail.text
    assert "secret raw extension" not in inbox_detail.text
    for forbidden in (
        "raw_body",
        "payload_sha256",
        "parsed_payload",
        "X-FulfillFlow-Signature",
        postgres_settings.carrier_alpha_webhook_secret.get_secret_value(),
    ):
        assert forbidden not in inbox_detail.text

    assert "SIMULATED" in notifications.text
    assert "FAILED" in notifications.text
    assert "Simulation recorded" in notifications.text
    assert "Simulation failed" in notifications.text
    assert "Simulation recorded. No email was sent." in simulated_detail.text
    assert "Resend" not in simulated_detail.text
    assert "Simulation failed. No email was sent." in failed_detail.text
    assert "Notification rendering or simulation failed." in failed_detail.text
    assert "Resend" not in failed_detail.text


async def test_api_and_webhook_need_no_csrf_and_create_no_session_cookie(
    ui_client: AsyncClient,
    postgres_settings: Settings,
) -> None:
    order = await create_order(ui_client, reference="UI-NO-COOKIE-0001")
    assert "fulfillflow_session" not in ui_client.cookies
    await confirm_order(ui_client, order["id"])
    shipment = await create_shipment(
        ui_client,
        order["id"],
        tracking_code="UINOCOOKIE0001",
    )
    response = await send_alpha_event(
        ui_client,
        postgres_settings,
        event_id="ui-no-cookie-event",
        tracking_code=shipment["tracking_code"],
        external_status="CREATED",
        occurred_at=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
    )

    assert response.status_code == 202
    assert "set-cookie" not in response.headers
    assert "fulfillflow_session" not in ui_client.cookies
