"""Async SQLAlchemy engine and request-scoped sessions."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create the process-wide engine. Called once from the app lifespan."""
    global _engine, _session_factory
    settings = settings or get_settings()
    if settings.debug:
        # Deliberately NOT ``echo=True``. That makes SQLAlchemy attach its own
        # handler to ``sqlalchemy.engine``, which still propagates to the root
        # handler the app installs — so every statement was printed twice, in two
        # different formats, which is most of why a debug log was unreadable.
        # Raising the level instead routes each statement through one handler.
        logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)

    _engine = create_async_engine(
        settings.database_url,
        echo=False,
        # Two defences, because they cover different failures. pre_ping tests a
        # connection as the pool hands it out, so one that died while idle is
        # replaced rather than used. recycle throws a connection away once it is
        # old enough that Azure's gateway might cut it at any moment — which is
        # the case pre_ping cannot catch, because the cut lands between the
        # check and the query.
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_size=5,
        max_overflow=10,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine, _session_factory = None, None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """The factory itself, for work that needs *independent* sessions.

    An AsyncSession is not safe for concurrent use, so anything fanning out
    queries in parallel needs one session per branch rather than sharing the
    request's. Exposed as a dependency so tests can substitute their own.
    """
    if _session_factory is None:
        raise RuntimeError("database engine not initialised")
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that commits on success, rolls back on error."""
    if _session_factory is None:
        raise RuntimeError("database engine not initialised")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
