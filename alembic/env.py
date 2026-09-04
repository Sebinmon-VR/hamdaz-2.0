"""Alembic environment — reads the URL from Settings so there is one source of truth."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app  # noqa: F401 — sets the Windows event-loop policy on import
from alembic import context
from app.core.config import get_settings
from app.models import Base  # noqa: F401 — registers every model on Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Kept out of the ini on purpose: configparser reads %% as interpolation, and a
# URL-encoded password (%%40 for @) makes it raise. Settings is the only source.
DATABASE_URL = get_settings().database_url
target_metadata = Base.metadata


def run_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,  # catch a column whose type changed, not just added/dropped
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
