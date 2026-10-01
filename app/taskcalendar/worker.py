"""The loop behind the task calendar: every ten minutes while it is on.

One instance at a time, settled by an advisory lock: two copies would each
create the same event.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.proposals.sharepoint import SharePointProposals
from app.taskcalendar import service
from app.taskcalendar.graph import TaskCalendar

logger = logging.getLogger("hamdaz.taskcalendar.worker")

#: Beside the others (812_401 – 812_406), used by nothing else.
CALENDAR_LOCK = 812_407
POLL_SECONDS = 600
BACKOFF_SECONDS = 900


class TaskCalendarWorker:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        sharepoint: SharePointProposals,
        calendar: TaskCalendar,
    ) -> None:
        self._factory = factory
        self._sharepoint = sharepoint
        self._calendar = calendar
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("task calendar worker started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _sleep(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass
        return not self._stopping.is_set()

    async def _loop(self) -> None:
        if not await self._sleep(90):
            return
        while not self._stopping.is_set():
            wait = POLL_SECONDS
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("task calendar sync failed")
                wait = BACKOFF_SECONDS
            if not await self._sleep(wait):
                return

    async def run_once(self) -> None:
        async with self._factory() as session:
            row = await service.get_settings(session)
            if not row.enabled:
                await session.commit()
                return
            got = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": CALENDAR_LOCK})
            if not got:
                await session.commit()
                return
            report = await service.sync(session, sharepoint=self._sharepoint, calendar=self._calendar)
            await session.commit()
            if report.created or report.updated or report.moved or report.removed:
                logger.info("task calendar: %s", report.as_dict())
