"""Real-owner UI observations stay separate from Tracking/Order completion."""

import pytest
from sqlalchemy import select, update

from fulfillflow.contracts.messages import decode_message
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging.store import put_message
from fulfillflow.notifications.message_tables import tables as notification_tables
from tests.api.test_notifications_http import applied
from tests.async_flow import drain_notifications


@pytest.mark.parametrize(
    "state", ["pending", "publication_blocked", "processing_blocked", "terminal"]
)
async def test_notification_observation_controls_and_terminal_precedence(
    ui_client,
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    postgres_notifications_database,
    fixed_clock,
    state,
):
    event, shipment = await applied(
        ui_client, postgres_settings, postgres_database, postgres_tracking_database, fixed_clock
    )
    if state == "terminal":
        await drain_notifications(postgres_database, fixed_clock)
    if state in ("publication_blocked", "terminal"):
        async with postgres_database.session() as session, session.begin():
            await session.execute(
                update(core_tables.outbox)
                .where(core_tables.outbox.c.type == "shipment.status_changed.v1")
                .values(state="BLOCKED", reason="TEST_PRESENTATION")
            )
    if state == "processing_blocked":
        async with postgres_database.session() as session:
            body = await session.scalar(
                select(core_tables.outbox.c.body).where(
                    core_tables.outbox.c.type == "shipment.status_changed.v1"
                )
            )
        async with postgres_notifications_database.session() as session, session.begin():
            await put_message(
                session, notification_tables.inbox, decode_message(body), fixed_clock.now()
            )
            await session.execute(
                update(notification_tables.inbox).values(
                    state="BLOCKED", reason="TEST_PRESENTATION"
                )
            )
    page = await ui_client.get(f"/notification-status/{event}", headers={"HX-Request": "true"})
    assert page.status_code == 200
    assert 'data-observation-renew="notification-' in page.text
    assert "hx-post" not in page.text
    assert "It never resends the webhook or rearms work" in page.text
    assert ('hx-trigger="observe"' in page.text) == (state == "pending")
    assert ('data-observation-poll="true"' in page.text) == (state == "pending")
    if state == "terminal":
        assert "Simulation recorded" in page.text
        assert "Notification work is blocked" not in page.text
    elif state.endswith("blocked"):
        assert "requires audited recovery" in page.text
    else:
        assert "Notification simulation is pending" in page.text
    assert "DELIVERED" in (await ui_client.get(f"/shipments/{shipment}")).text
    # Renewing this route is only a read; the original identities/counts stay fixed.
    assert (await ui_client.get(f"/notification-status/{event}")).status_code == 200
    async with postgres_database.session() as session:
        assert len((await session.execute(select(core_tables.outbox))).all()) == 2


async def test_notification_observation_failure_is_503_fragment_not_empty_success(
    ui_client,
    ui_application,
    postgres_settings,
    postgres_database,
    postgres_tracking_database,
    fixed_clock,
):
    import httpx

    event, _ = await applied(
        ui_client, postgres_settings, postgres_database, postgres_tracking_database, fixed_clock
    )

    async def unavailable(request):
        raise httpx.ConnectError("synthetic unreachable owner")

    original = ui_application.state.notifications_client
    async with httpx.AsyncClient(
        base_url="http://notifications", transport=httpx.MockTransport(unavailable)
    ) as remote:
        ui_application.state.notifications_client = remote
        try:
            for path in (f"/notification-status/{event}", "/notifications"):
                result = await ui_client.get(path, headers={"HX-Request": "true"})
                assert result.status_code == 503
                assert "No Notifications match" not in result.text
            dashboard = await ui_client.get("/")
            assert dashboard.status_code == 200
            assert "Notifications counts are unavailable" in dashboard.text
            assert "FULFILLED" in dashboard.text
        finally:
            ui_application.state.notifications_client = original
