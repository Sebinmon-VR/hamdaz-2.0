"""Alembic environment.

The database URL comes from :mod:`app.core.config`, not from ``alembic.ini`` — one source of
truth for configuration, so a migration can never run against a different database than the
app does.

It is deliberately **never** written into Alembic's config object. That config is backed by
``configparser``, which treats ``%`` as interpolation syntax, so a URL-encoded password
(``admin%40hmdz1``) raises ``ValueError: invalid interpolation syntax``. Building the engine
directly from settings sidesteps the whole problem rather than escaping around it.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.compat import run as run_async
from app.core.config import get_settings

# Importing the models package registers every table on Base.metadata. A model that is not
# exported from app.models is a model autogenerate will silently never see.
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return str(get_settings().database_url)


def _include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    """Keep autogenerate focused on our own schema."""
    return not (type_ == "table" and name in {"spatial_ref_sys"})


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # NullPool: a migration is one short-lived connection, and pooling here would just hold
    # a slot open against a server that has few to spare.
    connectable = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    # run_async, not asyncio.run: psycopg needs a selector loop on Windows.
    run_async(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
