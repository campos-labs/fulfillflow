"""Asynchronous PostgreSQL engine and session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fulfillflow.config import DatabaseSettings


def postgres_connect_args(settings: DatabaseSettings) -> dict[str, str]:
    """Build psycopg connection options shared by runtime and migrations."""
    return {"options": f"-c statement_timeout={settings.db_statement_timeout_ms}"}


@dataclass(frozen=True, slots=True)
class Database:
    """Own the async engine and request-scoped session factory."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> Database:
        """Create a PostgreSQL async engine without opening a connection."""
        engine = create_async_engine(
            settings.database_dsn,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_pre_ping=True,
            connect_args=postgres_connect_args(settings),
        )
        return cls(
            engine=engine,
            session_factory=async_sessionmaker(
                bind=engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            ),
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield one session without owning a business transaction boundary."""
        async with self.session_factory() as session:
            yield session

    async def ping(self) -> None:
        """Execute the minimal readiness query."""
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        """Release all pooled connections during shutdown."""
        await self.engine.dispose()
