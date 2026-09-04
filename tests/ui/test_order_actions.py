"""Order action visibility and defensive HTTP contracts in every Order state."""

from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from httpx import AsyncClient

from fulfillflow.config import Settings
from fulfillflow.orders.public import OrderStatus
from tests.support import FixedClock
from tests.ui.support import (
    confirm_order,
    create_order,
    create_shipment,
    csrf_token,
    send_alpha_event,
)


class _ActionTargets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: set[tuple[str, str]] = set()
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.forms.add(
                (
                    (attributes.get("method") or "get").lower(),
                    attributes.get("action") or "",
                )
            )
        elif tag == "a":
            self.links.add(attributes.get("href") or "")


async def _order_in_state(
    client: AsyncClient,
    settings: Settings,
    clock: FixedClock,
    status: OrderStatus,
) -> str:
    order = await create_order(client, reference=f"UI-ACTIONS-{status.value}")
    order_id = str(order["id"])
    if status is OrderStatus.CANCELLED:
        cancelled = await client.post(f"/api/v1/orders/{order_id}/cancel")
        assert cancelled.status_code == 200
    elif status in {OrderStatus.CONFIRMED, OrderStatus.FULFILLED}:
        await confirm_order(client, order_id)
        if status is OrderStatus.FULFILLED:
            shipment = await create_shipment(client, order_id)
            delivered = await send_alpha_event(
                client,
                settings,
                event_id="ui-order-actions-delivered",
                tracking_code=shipment["tracking_code"],
                external_status="DELIVERED",
                occurred_at=clock.now(),
            )
            assert delivered.status_code == 200, delivered.text
            assert delivered.json()["result"] == "APPLIED"
    persisted = await client.get(f"/api/v1/orders/{order_id}")
    assert persisted.status_code == 200
    assert persisted.json()["status"] == status.value
    return order_id


@pytest.mark.parametrize("htmx", [False, True], ids=["page", "fragment"])
@pytest.mark.parametrize(
    ("status", "mutations", "create_shipment_allowed"),
    [
        (OrderStatus.CREATED, {"confirm", "cancel"}, False),
        (OrderStatus.CONFIRMED, set(), True),
        (OrderStatus.FULFILLED, set(), False),
        (OrderStatus.CANCELLED, set(), False),
    ],
)
async def test_order_detail_only_offers_available_actions(
    ui_client: AsyncClient,
    postgres_settings: Settings,
    fixed_clock: FixedClock,
    status: OrderStatus,
    mutations: set[str],
    create_shipment_allowed: bool,
    htmx: bool,
) -> None:
    order_id = await _order_in_state(ui_client, postgres_settings, fixed_clock, status)
    response = await ui_client.get(
        f"/orders/{order_id}",
        headers={"HX-Request": "true"} if htmx else {},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "HX-Request" in response.headers["Vary"]
    assert ("<!doctype html>" in response.text) is not htmx

    targets = _ActionTargets()
    targets.feed(response.text)
    assert targets.forms == {("post", f"/orders/{order_id}/{action}") for action in mutations}
    shipment_links = {href for href in targets.links if urlsplit(href).path == "/shipments/new"}
    assert shipment_links == (
        {f"/shipments/new?order_id={order_id}"} if create_shipment_allowed else set()
    )
    if mutations:
        assert csrf_token(response)


async def test_fulfilled_order_still_rejects_direct_invalid_actions(
    ui_client: AsyncClient,
    postgres_settings: Settings,
    fixed_clock: FixedClock,
) -> None:
    order_id = await _order_in_state(
        ui_client, postgres_settings, fixed_clock, OrderStatus.FULFILLED
    )
    token = csrf_token(await ui_client.get("/orders/new"))
    request_id = "00000000-0000-4000-8000-000000000778"

    for action, target in (("confirm", "CONFIRMED"), ("cancel", "CANCELLED")):
        detail = f"Transition FULFILLED -> {target} is not allowed."
        api_response = await ui_client.post(
            f"/api/v1/orders/{order_id}/{action}",
            headers={"X-Request-ID": request_id},
        )
        assert api_response.status_code == 409
        assert api_response.headers["content-type"].startswith("application/problem+json")
        assert api_response.json()["code"] == "INVALID_ORDER_TRANSITION"
        assert api_response.json()["detail"] == detail
        assert api_response.json()["request_id"] == request_id

        html_response = await ui_client.post(
            f"/orders/{order_id}/{action}",
            data={"csrf_token": token},
            headers={"X-Request-ID": request_id},
        )
        assert html_response.status_code == 409
        assert html_response.headers["content-type"].startswith("text/html")
        assert "INVALID_ORDER_TRANSITION" in html_response.text
        assert request_id in html_response.text

    unchanged = await ui_client.get(f"/api/v1/orders/{order_id}")
    assert unchanged.json()["status"] == "FULFILLED"
