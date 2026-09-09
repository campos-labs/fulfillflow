"""Asynchronous Alembic environment for FulfillFlow."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from fulfillflow.asyncio_support import run_async
from fulfillflow.carriers import models as carrier_models  # noqa: F401
from fulfillflow.config import DatabaseSettings
from fulfillflow.db.base import Base
from fulfillflow.db.session import postgres_connect_args
from fulfillflow.notifications import models as notification_models  # noqa: F401
from fulfillflow.orders import models as order_models  # noqa: F401
from fulfillflow.shipments import models as shipment_models  # noqa: F401
from fulfillflow.shipments import receipt_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""
    settings = DatabaseSettings()
    context.configure(
        url=settings.database_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on the synchronous facade of an async connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations through SQLAlchemy's psycopg async engine."""
    settings = DatabaseSettings()
    connectable = create_async_engine(
        settings.database_dsn,
        poolclass=pool.NullPool,
        connect_args=postgres_connect_args(settings),
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_async(run_migrations_online())
