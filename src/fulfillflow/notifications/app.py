"""Independent Notifications query API; readiness depends only on its own schema and database."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.responses import Response

from fulfillflow import __version__
from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.db.migrations import SchemaNotCurrentError, schema_is_current
from fulfillflow.health import router as health_router
from fulfillflow.http.internal import install_internal_auth
from fulfillflow.http.problems import _problem, install_problem_handling
from fulfillflow.notifications.owned_service import (
    NotificationIntegrityError,
    OwnedNotificationNotFoundError,
)
from fulfillflow.notifications.router import router
from fulfillflow.shared import Clock, SystemClock


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    alembic_config_path: Path = Path("alembic_notifications.ini"),
    clock: Clock | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved = settings or Settings()
        if resolved.service_role != "notifications":
            raise ValueError("Notifications requires SERVICE_ROLE=notifications")
        owner_database = database or Database.from_settings(resolved)
        application.state.settings = resolved
        application.state.database = owner_database
        try:
            if not await schema_is_current(owner_database.engine, alembic_config_path):
                raise SchemaNotCurrentError(
                    "database schema does not match the current Alembic head"
                )
            application.state.schema_ready = True
            yield
        finally:
            application.state.schema_ready = False
            application.state.database = None
            await owner_database.dispose()

    application = FastAPI(title="FulfillFlow Notifications", version=__version__, lifespan=lifespan)
    application.state.database = None
    application.state.schema_ready = False
    application.state.clock = clock or SystemClock()
    install_internal_auth(application)
    install_problem_handling(application)
    application.include_router(health_router)
    application.include_router(router)
    application.add_exception_handler(OwnedNotificationNotFoundError, _not_found)
    application.add_exception_handler(NotificationIntegrityError, _integrity_error)
    return application


async def _not_found(request: Request, exception: Exception) -> Response:
    return _problem(
        request,
        status_code=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail=str(exception),
    )


async def _integrity_error(request: Request, exception: Exception) -> Response:
    return _problem(
        request,
        status_code=503,
        code="SERVICE_UNAVAILABLE",
        title="Service unavailable",
        detail="Notifications state could not be verified.",
    )


app = create_app()
