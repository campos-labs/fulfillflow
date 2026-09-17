"""Finite, correlated Notifications reads with a distinct credential and safe errors."""

import asyncio
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from fulfillflow.contracts.problems import RemoteServiceUnavailableError, ServiceProblemError
from fulfillflow.core.notifications_client import NotificationsClient
from fulfillflow.http.internal import INTERNAL_TOKEN_HEADER


def notification_client(http, settings, *, timeout=2):
    return NotificationsClient(
        http,
        settings,
        UUID(int=1),
        [(b"traceparent", b"test-trace")],
        secret=SecretStr("independent-notifications-token"),
        timeout_seconds=timeout,
    )


async def test_notification_reads_use_explicit_destination_secret_and_filters(settings):
    observed = []

    def response(request):
        observed.append(request)
        return httpx.Response(200, json={"items": [], "page": 2, "page_size": 3, "total": 0})

    async with httpx.AsyncClient(
        base_url="http://notifications", transport=httpx.MockTransport(response)
    ) as http:
        result = await notification_client(http, settings).list(
            status="FAILED", page=2, page_size=3
        )
    assert result.items == []
    assert len(observed) == 1
    request = observed[0]
    assert request.url.host == "notifications"
    assert request.url.path == "/internal/v1/notifications"
    assert dict(request.url.params) == {"status": "FAILED", "page": "2", "page_size": "3"}
    assert request.headers[INTERNAL_TOKEN_HEADER] == "independent-notifications-token"
    assert request.headers["x-request-id"] == str(UUID(int=1))
    assert request.headers["traceparent"] == "test-trace"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"secret": "private"}),
        httpx.Response(403, json={"secret": "private"}),
        httpx.Response(404, json={"secret": "private"}),
        httpx.Response(503, text="private"),
        httpx.Response(200, text="private"),
        httpx.Response(200, json={"unexpected": "private"}),
    ],
)
async def test_invalid_peer_response_is_unavailable_never_empty_or_false_missing(
    settings, response
):
    async with httpx.AsyncClient(
        base_url="http://notifications", transport=httpx.MockTransport(lambda request: response)
    ) as http:
        with pytest.raises(RemoteServiceUnavailableError) as error:
            await notification_client(http, settings).list()
    assert "private" not in str(error.value)
    assert error.value.status_code == 503


async def test_total_timeout_does_not_retry_or_leak_transport_details(settings):
    calls = 0

    async def never_respond(request):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        base_url="http://notifications", transport=httpx.MockTransport(never_respond)
    ) as http:
        with pytest.raises(RemoteServiceUnavailableError):
            await notification_client(http, settings, timeout=0.01).counts()
    assert calls == 1


async def test_missing_detail_requires_valid_owner_problem(settings):
    missing = UUID(int=2)
    problem = {
        "type": "https://fulfillflow.local/problems/resource-not-found",
        "status": 404,
        "code": "RESOURCE_NOT_FOUND",
        "title": "Resource not found",
        "detail": f"Notification {missing} was not found.",
        "request_id": str(UUID(int=1)),
        "errors": [],
    }
    async with httpx.AsyncClient(
        base_url="http://notifications",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                404, json=problem, headers={"Content-Type": "application/problem+json"}
            )
        ),
    ) as http:
        with pytest.raises(ServiceProblemError) as error:
            await notification_client(http, settings).get(missing)
    assert error.value.status_code == 404
    assert error.value.detail == problem["detail"]
