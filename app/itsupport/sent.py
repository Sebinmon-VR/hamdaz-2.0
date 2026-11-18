"""What the agent has sent once, so it never sends it twice.

A daily ask, an alert about a task, a heads-up: each has a key naming it
(``status-ask:haleema@hamdaz.com:2026-11-18``, ``bcd-moved:1975:2026-11-22T08:15``).
Kept in ``itsupport_sent`` so a redeploy or a second process doesn't repeat
it; the older local file (``due.Asked``) reset on every deploy. Without the
table (the migration not run) it remembers in memory, for this process only.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.itsupport import ITSupportSent

logger = logging.getLogger("hamdaz.itsupport")


class Ledger:
    def __init__(self, factory: async_sessionmaker[AsyncSession] | None) -> None:
        self._factory = factory
        self._memory: set[str] = set()

    async def seen(self, key: str) -> bool:
        if key in self._memory:
            return True
        if self._factory is None:
            return False
        try:
            async with self._factory() as session:
                found = await session.scalar(
                    select(ITSupportSent.key).where(ITSupportSent.key == key[:300]))
        except Exception as exc:  # noqa: BLE001 - memory stands in
            logger.warning("it support: the sent ledger couldn't be read: %s", exc)
            return False
        if found:
            self._memory.add(key)
        return found is not None

    async def mark(self, key: str, *, email: str, kind: str,
                   detail: dict[str, Any] | None = None) -> None:
        self._memory.add(key)
        if self._factory is None:
            return
        try:
            async with self._factory() as session:
                await session.execute(
                    insert(ITSupportSent).values(key=key[:300], email=email.lower(), kind=kind,
                                                 detail=detail or {})
                    .on_conflict_do_nothing(index_elements=["key"]))
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - memory stands in
            logger.warning("it support: the sent ledger couldn't be written: %s", exc)

    async def forget_older(self, days: int = 30) -> None:
        """Keys older than this are dropped: nothing waits that long."""
        if self._factory is None:
            return
        try:
            async with self._factory() as session:
                await session.execute(delete(ITSupportSent).where(
                    ITSupportSent.sent_at < datetime.now(UTC) - timedelta(days=days)))
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("it support: old sent keys not cleared: %s", exc)
