"""Authentication and finite HTTP transport shared by service boundaries."""

import asyncio
import hmac
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel, ValidationError
from starlette.responses import Response

from fulfillflow.config import Settings
from fulfillflow.contracts.problems import RemoteServiceUnavailableError, ServiceProblemError
from fulfillflow.http.problems import ProblemDetail, _problem

INTERNAL_TOKEN_HEADER = "X-FulfillFlow-Internal-Token"


def install_internal_auth(application: FastAPI) -> None:
    """Reject internal requests before route dispatch or body decoding."""

    @application.middleware("http")
    async def authenticate_internal(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/internal/"):
            settings = cast(Settings, request.app.state.settings)
            values = request.headers.getlist(INTERNAL_TOKEN_HEADER)
            if len(values) != 1 or not hmac.compare_digest(
                values[0].encode("utf-8"), settings.internal_api_secret.get_secret_value().encode()
            ):
                return _problem(
                    request,
                    status_code=401,
                    code="INTERNAL_AUTHENTICATION_FAILED",
                    title="Internal authentication failed",
                    detail="Internal service authentication failed.",
                )
        return await call_next(request)


class ServiceClient:
    """Request-scoped correlation over a lifespan-owned asynchronous HTTP pool."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        request_id: UUID,
        trace_headers: Sequence[tuple[bytes, bytes]] = (),
    ) -> None:
        self._client = client
        self._settings = settings
        self._request_id = request_id
        self._trace_headers = list(trace_headers)

    async def request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: Sequence[tuple[str, str]] | Sequence[tuple[bytes, bytes]] = (),
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        outgoing = httpx.Headers(headers)
        outgoing.update(self._trace_headers)
        outgoing.update(
            [
                (INTERNAL_TOKEN_HEADER, self._settings.internal_api_secret.get_secret_value()),
                ("X-Request-ID", str(self._request_id)),
            ]
        )
        try:
            # HTTPX limits individual I/O operations; bound the whole hop as well.
            budget = (
                self._settings.service_http_timeout_seconds
                if self._settings.service_role == "tracking"
                else self._settings.forwarding_timeout_seconds
            )
            async with asyncio.timeout(budget):
                return await self._client.request(
                    method, path, content=content, headers=outgoing, params=params
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise RemoteServiceUnavailableError from exc

    async def read[T: BaseModel](
        self,
        method: str,
        path: str,
        schema: type[T],
        *,
        payload: BaseModel | None = None,
        params: dict[str, str | int] | None = None,
    ) -> T:
        response = await self.request(
            method,
            path,
            content=payload.model_dump_json().encode() if payload is not None else None,
            headers=[("Content-Type", "application/json")] if payload is not None else (),
            params=params,
        )
        raise_for_service_problem(response)
        try:
            return schema.model_validate_json(response.content)
        except ValidationError as exc:
            raise RemoteServiceUnavailableError from exc


def raise_for_service_problem(response: httpx.Response) -> None:
    """Forward only a validated problem envelope from our authenticated peer."""
    if response.is_success:
        return
    try:
        problem = ProblemDetail.model_validate_json(response.content)
    except ValidationError as exc:
        raise RemoteServiceUnavailableError from exc
    if problem.status != response.status_code:
        raise RemoteServiceUnavailableError
    raise ServiceProblemError(
        status_code=problem.status,
        code=problem.code,
        title=problem.title,
        detail=problem.detail,
    )


def trace_headers(request: Request) -> list[tuple[bytes, bytes]]:
    return [
        (key, value) for key, value in request.headers.raw if key in {b"traceparent", b"tracestate"}
    ]


def query_params(values: dict[str, Any]) -> dict[str, str | int]:
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else str(value)
        for key, value in values.items()
        if value is not None
    }
