"""The loop behind the BCD check: every five minutes while it is on.

Each pass reads the watched people's tasks, opens a check on any new
placeholder, closes the ones corrected on the list, and — in working hours —
asks and escalates. One instance at a time, settled by an advisory lock;
the unique ``task_id`` is the second guard.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bcd import service
from app.bcd.mailer import BcdMailer
from app.core.config import Settings
from app.proposals.sharepoint import SharePointProposals

logger = logging.getLogger("hamdaz.bcd.worker")

#: Beside the others (812_401 – 812_405), used by nothing else.
BCD_LOCK = 812_406
POLL_SECONDS = 300
BACKOFF_SECONDS = 600


class BcdWorker:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        sharepoint: SharePointProposals,
        mailer: BcdMailer,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._sharepoint = sharepoint
        self._mailer = mailer
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    @property
    def mailer(self) -> BcdMailer:
        return self._mailer

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("BCD check worker started")

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
        if not await self._sleep(60):
            return
        while not self._stopping.is_set():
            wait = POLL_SECONDS
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("BCD check failed")
                wait = BACKOFF_SECONDS
            if not await self._sleep(wait):
                return

    async def run_once(self) -> None:
        async with self._factory() as session:
            row = await service.get_settings(session)
            if not row.enabled:
                await session.commit()
                return
            got = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": BCD_LOCK})
            if not got:
                await session.commit()
                return
            report = await service.run(
                session, settings=self._settings, sharepoint=self._sharepoint, mailer=self._mailer
            )
            await session.commit()
            if report.found or report.asked or report.escalated or report.resolved:
                logger.info("BCD checks: %s", report.as_dict())
