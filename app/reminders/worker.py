"""The loop that reminds: once a day, at the reminder time.

One instance sends, settled by a Postgres advisory lock — two copies would
each remind the same people. The unique ``(task_id, due_at)`` is the second
guard. Off, it reads the settings and sleeps; an error is written to the
settings row, where the screen shows it.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.followups import service as followups
from app.proposals.sharepoint import SharePointProposals
from app.reminders import service
from app.reminders.mailer import ReminderMailer

logger = logging.getLogger("hamdaz.reminders.worker")

#: Beside the intake's, the workflows' and Ariba's, and used by nothing else.
REMINDER_LOCK = 812_405

#: How often it looks at the clock. The reminders themselves go once a day.
POLL_SECONDS = 300
BACKOFF_SECONDS = 600


class ReminderWorker:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        sharepoint: SharePointProposals,
        mailer: ReminderMailer,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._sharepoint = sharepoint
        self._mailer = mailer
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    @property
    def mailer(self) -> ReminderMailer:
        return self._mailer

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("status reminder worker started")

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
        if not await self._sleep(45):
            return
        while not self._stopping.is_set():
            wait = POLL_SECONDS
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("status reminder run failed")
                wait = BACKOFF_SECONDS
            if not await self._sleep(wait):
                return

    async def run_once(self) -> None:
        from datetime import UTC, datetime

        async with self._factory() as session:
            row = await service.get_settings(session)
            fs = await followups.get_settings(session)
            if not service.is_due(fs, row, datetime.now(UTC)):
                await session.commit()
                return
            got = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": REMINDER_LOCK}
            )
            if not got:
                await session.commit()
                return
            report = await service.run(
                session, settings=self._settings, sharepoint=self._sharepoint, mailer=self._mailer
            )
            await session.commit()
            if report.ran:
                logger.info("status reminders: %s", report.as_dict())
