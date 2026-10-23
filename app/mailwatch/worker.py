"""The mail watch's loop: read when Graph says mail arrived, and nothing else.

It wakes for three reasons only:

* a **notification** — the webhook kicks it, and it reads from the bookmark;
* **start-up** — one catch-up, for mail that arrived while the server was down;
* the **hourly check of the subscription** — renewed before Graph drops it.
  That reads no mail.

One instance does the reading: a Postgres advisory lock settles it, as for the
mirror. A kick that finds the lock taken tries once more shortly after, since
the holder may have started reading just before the mail landed.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.model import AgentModel
from app.core.config import Settings
from app.intake.graph_mail import MailReader
from app.mailwatch import service, teams
from app.proposals.sharepoint import SharePointProposals

logger = logging.getLogger("hamdaz.mailwatch.worker")

#: Arbitrary, and constant. A number nobody else in this database uses.
MAILWATCH_LOCK = 812_407

#: How often the subscription's expiry is looked at. Reads no mail.
SUBSCRIPTION_CHECK_SECONDS = 3600

#: A kick that found the lock taken is retried once, after this.
RETRY_SECONDS = 15


async def _take_lock(session: AsyncSession) -> bool:
    got = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": MAILWATCH_LOCK}
    )
    return bool(got)


class MailWatchWorker:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        reader: MailReader,
        model: AgentModel | None,
        sharepoint: SharePointProposals | None,
        poster: teams.TeamsPoster | None = None,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._reader = reader
        self._model = model
        self._sharepoint = sharepoint
        self._poster = poster
        self._tasks: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()
        self._kick = asyncio.Event()

    def start(self) -> None:
        if self._tasks:
            return
        task = asyncio.create_task(self._loop())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self._stopping.set()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    def kick(self) -> None:
        """Read at once. A flag, not a call: Graph wants its answer in seconds,
        and five notifications for one burst of mail should cost one read."""
        self._kick.set()

    async def _wait(self, seconds: float) -> bool | None:
        """True when kicked, False on the timer, None when stopping."""
        waiters = [
            asyncio.ensure_future(self._stopping.wait()),
            asyncio.ensure_future(self._kick.wait()),
        ]
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        if self._stopping.is_set():
            return None
        kicked = self._kick.is_set()
        self._kick.clear()
        return kicked

    async def _loop(self) -> None:
        # The start-up catch-up, after a moment so it is not competing with boot.
        if await self._wait(15) is None:
            return
        await self._run(read=True, subscription=True)
        while True:
            kicked = await self._wait(SUBSCRIPTION_CHECK_SECONDS)
            if kicked is None:
                return
            if kicked:
                if not await self._run(read=True, subscription=False):
                    if await self._wait(RETRY_SECONDS) is None:
                        return
                    await self._run(read=True, subscription=False)
            else:
                await self._run(read=False, subscription=True)

    async def _run(self, *, read: bool, subscription: bool) -> bool:
        """One pass. False when another instance held the lock."""
        try:
            async with self._factory() as session:
                if not await _take_lock(session):
                    return False
                row = await service.get_settings(session)
                if not row.enabled:
                    await session.commit()
                    return True
                if subscription:
                    await service.keep_subscribed(
                        session, reader=self._reader, settings=self._settings
                    )
                    if self._poster is not None:
                        await self._poster.keep_alive(await teams.get_connection(session))
                if read:
                    report = await service.read_new(
                        session, reader=self._reader, model=self._model,
                        sharepoint=self._sharepoint,
                    )
                    if report.error:
                        logger.warning("mail watch read: %s", report.error)
                await session.commit()
                if read and self._poster is not None:
                    # After the commit, so a post never names a mail that
                    # was then rolled back; rows are locked one by one.
                    await teams.post_pending(
                        session, poster=self._poster, settings=self._settings
                    )
                    await session.commit()
        except Exception:  # noqa: BLE001 - a loop must not die
            logger.exception("mail watch pass failed")
        return True
