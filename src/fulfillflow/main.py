"""FastAPI application composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from fulfillflow import __version__
from fulfillflow.api.problems import install_problem_handling
from fulfillflow.api.router import router as api_router
from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.db.migrations import SchemaNotCurrentError, schema_is_current
from fulfillflow.health import router as health_router
from fulfillflow.shared import Clock, SystemClock

DEFAULT_ALEMBIC_CONFIG_PATH = Path("alembic.ini")


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    alembic_config_path: Path = DEFAULT_ALEMBIC_CONFIG_PATH,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the application while deferring environment validation to startup."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or Settings()
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
    install_problem_handling(application)
    application.include_router(health_router)
    application.include_router(api_router)
    return application


app = create_app()
