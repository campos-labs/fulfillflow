"""FastAPI application composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
from fastapi import FastAPI, Request
from starlette.responses import Response

from fulfillflow import __version__
from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.db.migrations import SchemaNotCurrentError, schema_is_current
from fulfillflow.health import router as health_router
from fulfillflow.http.internal import install_internal_auth
from fulfillflow.http.problems import _problem, install_problem_handling
from fulfillflow.shared import Clock, SystemClock
from fulfillflow.tracking.public import CarrierEventNotFoundError
from fulfillflow.tracking.router import router as internal_router

DEFAULT_ALEMBIC_CONFIG_PATH = Path("alembic_tracking.ini")


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    alembic_config_path: Path = DEFAULT_ALEMBIC_CONFIG_PATH,
    clock: Clock | None = None,
    service_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the application while deferring environment validation to startup."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or Settings()
        if resolved_settings.service_role != "tracking":
            raise ValueError("Tracking requires SERVICE_ROLE=tracking")
        resolved_database = database or Database.from_settings(resolved_settings)

        application.title = resolved_settings.app_name
        application.state.settings = resolved_settings
        application.state.database = resolved_database
        application.state.schema_ready = False

        try:
            if not await schema_is_current(resolved_database.engine, alembic_config_path):
                raise SchemaNotCurrentError(
                    "database schema does not match the current Alembic head"
                )
            application.state.schema_ready = True
            async with httpx.AsyncClient(
                base_url=str(resolved_settings.core_base_url),
                timeout=httpx.Timeout(resolved_settings.service_http_timeout_seconds),
                transport=cast(
                    httpx.AsyncBaseTransport | None, application.state.service_transport
                ),
                trust_env=False,
            ) as service_client:
                application.state.service_client = service_client
                yield
        finally:
            application.state.schema_ready = False
            application.state.database = None
            await resolved_database.dispose()

    application = FastAPI(
        title=settings.app_name if settings is not None else "FulfillFlow",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.database = None
    application.state.schema_ready = False
    application.state.clock = clock or SystemClock()
    application.state.service_transport = service_transport
    install_internal_auth(application)
    install_problem_handling(application)
    application.include_router(health_router)
    application.include_router(internal_router)
    application.add_exception_handler(CarrierEventNotFoundError, _not_found)
    return application


async def _not_found(request: Request, exception: Exception) -> Response:
    return _problem(
        request,
        status_code=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail=str(exception),
    )


app = create_app()
