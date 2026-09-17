"""Exercise both real FastAPI services through HTTP transports and separate engines."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import SecretStr

from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.main import create_app as create_core
from fulfillflow.notifications.app import create_app as create_notifications
from fulfillflow.shared import Clock
from fulfillflow.tracking.app import create_app as create_tracking


def create_app(
    settings: Settings,
    database: Database,
    alembic_config_path: Path = Path("alembic_core.ini"),
    clock: Clock | None = None,
) -> FastAPI:
    """Keep real serialization/auth/request lifecycles while avoiding a TCP port per test."""
    core = create_core(settings, database, alembic_config_path, clock)
    tracking_settings = settings.model_copy(
        update={
            "database_url": SecretStr(os.environ["TEST_TRACKING_DATABASE_URL"]),
            "service_role": "tracking",
        }
    )
    tracking = create_tracking(
        tracking_settings,
        clock=clock,
        service_transport=ASGITransport(app=core, raise_app_exceptions=False),
    )
    core.state.service_transport = ASGITransport(app=tracking, raise_app_exceptions=False)
    core.state.tracking_app = tracking
    notifications_settings = settings.model_copy(
        update={
            "database_url": SecretStr(os.environ["TEST_NOTIFICATIONS_DATABASE_URL"]),
            "service_role": "notifications",
            "internal_api_secret": settings.notifications_api_secret,
        }
    )
    notifications = create_notifications(notifications_settings, clock=clock)
    core.state.notifications_transport = ASGITransport(
        app=notifications, raise_app_exceptions=False
    )
    core.state.notifications_app = notifications
    original_lifespan = core.router.lifespan_context

    @asynccontextmanager
    async def pair_lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with (
            original_lifespan(application),
            tracking.router.lifespan_context(tracking),
            notifications.router.lifespan_context(notifications),
        ):
            yield

    core.router.lifespan_context = pair_lifespan
    return core
