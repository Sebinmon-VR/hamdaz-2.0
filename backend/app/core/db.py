"""Async SQLAlchemy engine and session management.

Root cause #3 in the audit is that the legacy app used module-level mutable state as its
database — ``tasks``, ``df`` and ``user_analytics`` were globals rewritten by a background
thread, so every gunicorn worker held a divergent copy. Nothing here is global but the
engine, and the engine is stateless.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        str(settings.database_url),
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,  # survive Postgres restarts and idle-connection reaping
        future=True,
    )


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create the process-wide engine. Called once from the app/worker lifespan."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(settings or get_settings())
        _session_factory = async_sessionmaker(
            _engine,
            class_=AsyncSession,
            expire_on_commit=False,  # keep objects usable after commit in request handlers
            autoflush=False,
        )
    return _engine


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session bound to the request.

    The session is committed on success and rolled back on any exception, so a handler that
    raises cannot leave a half-written transaction behind.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Same contract as :func:`get_db`, for workers and scripts that are not requests."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
