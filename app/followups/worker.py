"""The loop that asks: re-read the watched people's tasks every few minutes.

One instance runs it, settled by a Postgres advisory lock, for the same reason
the intake's loops are: two copies would each decide a task is newly overdue
and mail the person twice. The unique ``(task_id, due_at)`` on the follow-ups
table is the second guard; the lock is what keeps it from being the first.

Quiet by design. Off, it reads the settings and sleeps. On, an error leaves
everything as it was and is written to the settings row, where the screen
shows it.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.followups import service
from app.followups.mailer import FollowupMailer
from app.proposals.sharepoint import SharePointProposals

logger = logging.getLogger("hamdaz.followups.worker")

#: Beside the intake's 812_401 and 812_402, and used by nothing else.
FOLLOWUP_LOCK = 812_403

BACKOFF_SECONDS = 300


class FollowupWorker:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        sharepoint: SharePointProposals,
        mailer: FollowupMailer,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._sharepoint = sharepoint
        self._mailer = mailer
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    @property
    def mailer(self) -> FollowupMailer:
        return self._mailer

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("follow-up worker started")

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
        if not await self._sleep(30):
            return
        while not self._stopping.is_set():
            wait = 120
            try:
                wait = await self.run_once()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("follow-up sweep failed")
                wait = BACKOFF_SECONDS
            if not await self._sleep(wait):
                return

    async def run_once(self, *, force: bool = False) -> int:
        """One sweep if it is on (or forced). Returns how long to wait next."""
        async with self._factory() as session:
            row = await service.get_settings(session)
            wait = max(30, row.poll_seconds)
            if not (row.enabled or force):
                await session.commit()
                return max(wait, 120)
            got = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": FOLLOWUP_LOCK}
            )
            if not got:
                await session.commit()
                return wait
            report = await service.sweep(
                session,
                settings=self._settings,
                sharepoint=self._sharepoint,
                mailer=self._mailer,
                force=force,
            )
            await session.commit()
            if report.asked or report.resolved:
                logger.info("follow-ups: %s", report.as_dict())
            self.last_report = report.as_dict()
            return wait

    last_report: dict | None = None
