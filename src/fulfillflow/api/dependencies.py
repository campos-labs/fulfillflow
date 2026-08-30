"""Request-scoped application dependencies."""

from collections.abc import AsyncIterator
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.db import Database
from fulfillflow.shared import Clock


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield the single AsyncSession allocated to one HTTP request."""
    database = cast(Database | None, getattr(request.app.state, "database", None))
    if database is None:
        raise RuntimeError("application database is unavailable before startup")
    async with database.session() as session:
        yield session


def get_clock(request: Request) -> Clock:
    """Return the application clock configured at composition time."""
    return cast(Clock, request.app.state.clock)
