"""Page, navigation, fragment and static-asset smoke coverage."""

from __future__ import annotations

from uuid import UUID

from httpx import AsyncClient


async def test_empty_pages_navigation_fragments_and_local_assets(
    ui_client: AsyncClient,
) -> None:
    pages = {
        "/": "Operational dashboard",
        "/orders": "No Orders match the current filters.",
        "/orders/new": "Create Order",
        "/shipments": "No Shipments match the current filters.",
        "/shipments/new": "Create Shipment",
        "/carrier-events": "No inbox events match the current filters.",
        "/notifications": "No Notifications match the current filters.",
        "/simulator": "This page is instructional.",
    }
    for path, marker in pages.items():
        response = await ui_client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert response.headers["content-type"].startswith("text/html")
        assert marker in response.text
        assert "Dashboard" in response.text
        assert "Orders" in response.text
        assert "Shipments" in response.text
        assert "Inbox" in response.text
        assert "Notifications" in response.text
        assert "Simulator" in response.text
        for navigation_path in (
            "/",
            "/orders",
            "/shipments",
            "/carrier-events",
            "/notifications",
            "/simulator",
        ):
            assert f'href="{navigation_path}"' in response.text
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "private, no-store"
        assert 'name="htmx-config"' in response.text
        assert '"includeIndicatorStyles": false' in response.text
        assert '"allowEval": false' in response.text

    order_id = UUID("00000000-0000-4000-8000-000000000777")
    prefilled = await ui_client.get(f"/shipments/new?order_id={order_id}")
    assert f'value="{order_id}"' in prefilled.text
    invalid_prefill = await ui_client.get("/shipments/new?order_id=not-a-uuid")
    assert invalid_prefill.status_code == 422
    assert "VALIDATION_ERROR" in invalid_prefill.text

    fragment = await ui_client.get("/orders", headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    assert fragment.headers["Vary"] == "HX-Request"
    assert '<main id="main-content"' in fragment.text
    assert "<!doctype html>" not in fragment.text
    assert "<nav" not in fragment.text
    assert fragment.headers["Cache-Control"] == "private, no-store"

    css = await ui_client.get("/static/vendor/bootstrap-5.3.8.min.css")
    htmx = await ui_client.get("/static/vendor/htmx-2.0.10.min.js")
    application_css = await ui_client.get("/static/fulfillflow-v1.css")
    assert css.status_code == htmx.status_code == application_css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "javascript" in htmx.headers["content-type"]
    assert css.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert application_css.headers["Cache-Control"] == "public, max-age=3600"


async def test_simulator_panel_contains_no_execution_route_or_secret_value(
    ui_client: AsyncClient,
) -> None:
    response = await ui_client.get("/simulator")

    assert response.status_code == 200
    for scenario in (
        "valid",
        "duplicate",
        "out-of-order",
        "unknown-status",
        "invalid-signature",
    ):
        assert scenario in response.text
    assert "Carrier Alpha" in response.text
    assert "Carrier Beta" in response.text
    assert "scripts/simulate_carrier_events.py" in response.text
    assert "&lt;set-locally&gt;" in response.text
    assert "/simulator/run" not in response.text
    assert "sha256=" not in response.text
    assert "alpha-test-value" not in response.text
    assert "beta-test-value" not in response.text
