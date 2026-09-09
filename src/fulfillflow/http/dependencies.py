"""Technical request dependencies independent of either business implementation."""

from collections.abc import AsyncIterator
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.shared import Clock


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    database = cast(Database | None, getattr(request.app.state, "database", None))
    if database is None:
        raise RuntimeError("application database is unavailable before startup")
    async with database.session() as session:
        yield session


def get_clock(request: Request) -> Clock:
    return cast(Clock, request.app.state.clock)


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)
