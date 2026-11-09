"""Switches off sign-in for leavers once their last working day has passed.

Every 30 minutes, so a last day ending at midnight is acted on within the half
hour. Exclusion from assignment needs no loop at all — it is read from the exit
record each time a ranking is built — so this is only about the account. One
instance at a time, settled by an advisory lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.hr import staff

logger = logging.getLogger("hamdaz.hr.staff_worker")

#: Beside the others (812_401 – 812_408), used by nothing else.
STAFF_LOCK = 812_409
POLL_SECONDS = 1800


class StaffWorker:
    def __init__(self, *, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("staff worker started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _sleep(self, seconds: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        return not self._stopping.is_set()

    async def _loop(self) -> None:
        if not await self._sleep(90):
            return
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("staff sweep failed")
            if not await self._sleep(POLL_SECONDS):
                return

    async def run_once(self) -> list[str]:
        async with self._factory() as session:
            got = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": STAFF_LOCK}
            )
            if not got:
                await session.commit()
                return []
            ended = await staff.end_access_for_leavers(session)
            names = [u.display_name for u in ended]
            await session.commit()
            if names:
                logger.info("sign-in switched off for leavers: %s", ", ".join(names))
                # They were excluded already; this drops them from the stored
                # standing as switched off rather than as serving notice.
                await staff.refresh_rankings(session, reason="leaver")
                await session.commit()
            return names
