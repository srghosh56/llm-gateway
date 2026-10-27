"""Alembic environment: runs migrations with the app's async engine and settings."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.config import Settings
from gateway.db import Base

config = context.config
if config.config_file_name is not None:
    # Don't disable loggers that already exist (e.g. the app's, when migrating in-process).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The ORM metadata is the single source of truth; `alembic revision --autogenerate` diffs it.
target_metadata = Base.metadata
# Same env var as the app, so migrations can never target a different database by accident.
database_url = Settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without a DB connection."""
    context.configure(url=database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
