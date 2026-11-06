"""FastAPI application composition root."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
from fastapi import FastAPI

from fulfillflow import __version__
from fulfillflow.api.problems import install_problem_handling
from fulfillflow.api.router import router as api_router
from fulfillflow.config import Settings
from fulfillflow.core.router import router as internal_router
from fulfillflow.db import Database
from fulfillflow.db.migrations import SchemaNotCurrentError, schema_is_current
from fulfillflow.health import router as health_router
from fulfillflow.http.internal import install_internal_auth
from fulfillflow.http.telemetry import DiagnosticMiddleware, create_provider, tracer_for
from fulfillflow.shared import Clock, SystemClock
from fulfillflow.web import install_web

DEFAULT_ALEMBIC_CONFIG_PATH = Path("alembic_core.ini")


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    alembic_config_path: Path = DEFAULT_ALEMBIC_CONFIG_PATH,
    clock: Clock | None = None,
    service_transport: httpx.AsyncBaseTransport | None = None,
    notifications_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the application while deferring environment validation to startup."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or Settings()
        if resolved_settings.service_role != "core":
            raise ValueError("Core requires SERVICE_ROLE=core")
        resolved_database = database or Database.from_settings(resolved_settings)

        application.title = resolved_settings.app_name
        application.state.settings = resolved_settings
        application.state.database = resolved_database
        application.state.schema_ready = False

        provider = None
        try:
            provider = create_provider(resolved_settings)
            application.state.http_tracer = tracer_for(provider)
            if not await schema_is_current(resolved_database.engine, alembic_config_path):
                raise SchemaNotCurrentError(
                    "database schema does not match the current Alembic head"
                )
            application.state.schema_ready = True
            async with (
                httpx.AsyncClient(
                    base_url=str(resolved_settings.tracking_base_url),
                    timeout=httpx.Timeout(resolved_settings.forwarding_timeout_seconds),
                    transport=cast(
                        httpx.AsyncBaseTransport | None, application.state.service_transport
                    ),
                    trust_env=False,
                ) as service_client,
                httpx.AsyncClient(
                    base_url=str(resolved_settings.notifications_base_url),
                    timeout=httpx.Timeout(resolved_settings.notifications_http_timeout_seconds),
                    transport=cast(
                        httpx.AsyncBaseTransport | None, application.state.notifications_transport
                    ),
                    trust_env=False,
                ) as notifications_client,
            ):
                application.state.service_client = service_client
                application.state.notifications_client = notifications_client
                yield
        finally:
            application.state.schema_ready = False
            application.state.database = None
            try:
                await resolved_database.dispose()
            finally:
                application.state.http_tracer = None
                if provider is not None:
                    await asyncio.to_thread(provider.shutdown)

    application = FastAPI(
        title=settings.app_name if settings is not None else "FulfillFlow",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.database = None
    application.state.schema_ready = False
    application.state.http_tracer = None
    application.add_middleware(DiagnosticMiddleware)
    application.state.clock = clock or SystemClock()
    application.state.service_transport = service_transport
    application.state.notifications_transport = notifications_transport
    install_internal_auth(application)
    install_problem_handling(application)
    application.include_router(health_router)
    application.include_router(api_router)
    application.include_router(internal_router)
    install_web(application)
    return application


app = create_app()
