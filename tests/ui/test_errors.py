"""HTML 404, 405, query validation and database failure behavior."""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError

from fulfillflow.orders.public import OrderService


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/not-a-web-route", 404),
        ("GET", f"/orders/{uuid4()}", 404),
        ("DELETE", "/orders", 405),
        ("GET", "/orders?page=zero", 422),
        ("GET", "/shipments?unexpected=value", 422),
    ],
)
async def test_web_errors_are_sanitized_html_with_request_id(
    ui_client: AsyncClient,
    method: str,
    path: str,
    status: int,
) -> None:
    response = await ui_client.request(method, path)

    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/html")
    assert "Request ID:" in response.text
    assert response.headers["X-Request-ID"] in response.text
    assert "Traceback" not in response.text


async def test_database_unavailable_is_not_rendered_as_success(
    ui_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OperationalError("SELECT", {}, RuntimeError("database offline"))

    monkeypatch.setattr(OrderService, "list", unavailable)
    response = await ui_client.get("/orders")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert "DATABASE_UNAVAILABLE" in response.text
    assert "database offline" not in response.text
    assert "Request ID:" in response.text
