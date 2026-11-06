"""RFC 9457-style problem details and request correlation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from fulfillflow.contracts.problems import ServiceProblemError

_PROBLEM_BASE = "https://fulfillflow.local/problems"


class ProblemDetail(BaseModel):
    """Stable error envelope used by every public API failure."""

    model_config = ConfigDict(extra="forbid")

    type: str
    title: str
    status: int
    code: str
    detail: str
    request_id: UUID
    errors: list[dict[str, Any]]


def install_problem_handling(application: FastAPI) -> None:
    """Install request-ID propagation and all global error translations."""

    @application.middleware("http")
    async def correlate_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = _valid_or_new_request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(request_id)
        return response

    application.add_exception_handler(ServiceProblemError, _tracking_problem_handler)
    application.add_exception_handler(RequestValidationError, _validation_handler)
    application.add_exception_handler(StarletteHTTPException, _http_handler)
    application.add_exception_handler(SQLAlchemyError, _database_handler)
    application.add_exception_handler(Exception, _internal_handler)


async def _tracking_problem_handler(
    request: Request,
    exception: Exception,
) -> Response:
    if not isinstance(exception, ServiceProblemError):
        return await _internal_handler(request, exception)
    return _problem(
        request,
        status_code=exception.status_code,
        code=exception.code,
        title=exception.title,
        detail=exception.detail,
    )


async def _validation_handler(
    request: Request,
    exception: Exception,
) -> Response:
    validation_error = exception
    if not isinstance(validation_error, RequestValidationError):
        return await _internal_handler(request, exception)
    errors = [
        {
            "location": list(item["loc"]),
            "message": item["msg"],
            "error_type": item["type"],
        }
        for item in validation_error.errors()
    ]
    return _problem(
        request,
        status_code=422,
        code="VALIDATION_ERROR",
        title="Request validation failed",
        detail="The request contains invalid fields or parameters.",
        errors=errors,
    )


async def _http_handler(request: Request, exception: Exception) -> Response:
    if not isinstance(exception, StarletteHTTPException):
        return await _internal_handler(request, exception)
    if exception.status_code == 404:
        code, title, detail = "RESOURCE_NOT_FOUND", "Resource not found", "Route not found."
    elif exception.status_code == 405:
        code, title, detail = (
            "RESOURCE_NOT_FOUND",
            "Method not allowed",
            "The HTTP method is not allowed for this route.",
        )
    else:
        code, title, detail = "INTERNAL_ERROR", "HTTP request failed", str(exception.detail)
    return _problem(
        request,
        status_code=exception.status_code,
        code=code,
        title=title,
        detail=detail,
    )


async def _database_handler(request: Request, exception: Exception) -> Response:
    del exception
    return _problem(
        request,
        status_code=503,
        code="DATABASE_UNAVAILABLE",
        title="Database unavailable",
        detail="The database could not complete the request.",
    )


async def _internal_handler(request: Request, exception: Exception) -> Response:
    del exception
    return _problem(
        request,
        status_code=500,
        code="INTERNAL_ERROR",
        title="Internal server error",
        detail="The request could not be completed.",
    )


def _problem(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    errors: list[dict[str, Any]] | None = None,
) -> Response:
    slug = code.lower().replace("_", "-")
    problem = ProblemDetail(
        type=f"{_PROBLEM_BASE}/{slug}",
        title=title,
        status=status_code,
        code=code,
        detail=detail,
        request_id=request.state.request_id,
        errors=errors or [],
    )
    renderer = getattr(request.app.state, "web_problem_renderer", None)
    if callable(renderer) and _is_html_route(request.url.path):
        return cast(
            Response,
            renderer(
                request,
                status_code=status_code,
                code=code,
                title=title,
                detail=detail,
                errors=errors or [],
            ),
        )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json"),
        media_type="application/problem+json",
    )


def _valid_or_new_request_id(header: str | None) -> UUID:
    if header is not None:
        try:
            return UUID(header)
        except ValueError:
            pass
    return uuid4()


def _is_html_route(path: str) -> bool:
    """Keep API, health and generated documentation errors in JSON."""
    return not (
        path.startswith("/api/v1")
        or path.startswith("/internal/")
        or path.startswith("/health")
        or path.startswith("/docs")
        or path.startswith("/redoc")
        or path == "/openapi.json"
    )
