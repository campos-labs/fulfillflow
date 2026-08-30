"""Global public API error and request-ID behavior without database access."""

from collections.abc import AsyncIterator
from typing import cast

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.api.dependencies import get_session
from fulfillflow.main import create_app


async def test_unknown_route_uses_problem_detail_and_echoes_valid_request_id() -> None:
    app = create_app()
    request_id = "00000000-0000-4000-8000-000000000123"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/not-a-route", headers={"X-Request-ID": request_id})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["X-Request-ID"] == request_id
    assert response.json() == {
        "type": "https://fulfillflow.local/problems/resource-not-found",
        "title": "Resource not found",
        "status": 404,
        "code": "RESOURCE_NOT_FOUND",
        "detail": "Route not found.",
        "request_id": request_id,
        "errors": [],
    }


async def test_framework_validation_uses_problem_detail_and_new_request_id() -> None:
    app = create_app()

    async def unused_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, object())

    app.dependency_overrides[get_session] = unused_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/api/v1/orders/not-a-uuid",
            headers={"X-Request-ID": "not-a-uuid"},
        )

    body = response.json()
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["X-Request-ID"] != "not-a-uuid"
    assert body["code"] == "VALIDATION_ERROR"
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["errors"][0]["location"] == ["path", "order_id"]
