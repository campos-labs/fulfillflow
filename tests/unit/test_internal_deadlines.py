"""A whole-hop deadline bounds transports that never return a response."""

import asyncio
from uuid import uuid4

import httpx
import pytest

from fulfillflow.config import Settings
from fulfillflow.contracts.problems import RemoteServiceUnavailableError
from fulfillflow.http.internal import ServiceClient


@pytest.mark.parametrize("role", ["core", "tracking"])
async def test_whole_hop_timeout_cancels_transport(settings: Settings, role: str) -> None:
    cancelled = asyncio.Event()

    async def never_returns(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    settings = settings.model_copy(
        update={
            "service_role": role,
            "service_http_timeout_seconds": 0.01,
            "forwarding_timeout_seconds": 0.01,
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(never_returns), base_url="http://peer"
    ) as client:
        service = ServiceClient(client, settings, uuid4())
        with pytest.raises(RemoteServiceUnavailableError):
            await asyncio.wait_for(service.request("GET", "/internal/v1/example"), 1)
    assert cancelled.is_set()
