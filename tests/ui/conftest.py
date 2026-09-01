"""Real-PostgreSQL application client for UI tests."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.main import create_app
from tests.support import FixedClock


@pytest.fixture
async def ui_application(
    postgres_settings: Settings,
    postgres_database: Database,
    fixed_clock: FixedClock,
) -> AsyncIterator[FastAPI]:
    application = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def ui_client(ui_application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=ui_application),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        yield client
