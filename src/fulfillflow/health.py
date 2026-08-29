"""Operational liveness and readiness endpoints."""

from typing import Literal, cast

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from fulfillflow.db import Database

router = APIRouter(prefix="/health", tags=["health"])


class HealthStatus(BaseModel):
    """Deterministic health probe payload."""

    status: Literal["ok", "unavailable"]


def _unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=HealthStatus(status="unavailable").model_dump(),
    )


@router.get("/live", response_model=HealthStatus)
async def liveness() -> HealthStatus:
    """Report event-loop liveness without touching PostgreSQL."""
    return HealthStatus(status="ok")


@router.get(
    "/ready",
    response_model=HealthStatus,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthStatus}},
)
async def readiness(request: Request) -> Response:
    """Report readiness after the startup schema gate and a PostgreSQL ping."""
    schema_ready = bool(getattr(request.app.state, "schema_ready", False))
    database = cast(Database | None, getattr(request.app.state, "database", None))
    if not schema_ready or database is None:
        return _unavailable()

    try:
        await database.ping()
    except SQLAlchemyError:
        return _unavailable()

    return JSONResponse(content=HealthStatus(status="ok").model_dump())
