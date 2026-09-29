"""The loop that decides, every couple of minutes, whether Ariba needs a visit.

Each tick is one small query against the Proposals mirror — no portal, no
network beyond the database. Only when a new tender number has turned up, and
the burst has settled, and the gap and the daily cap allow it, does the loop
open the portal, once, for everything that arrived.

One instance runs it, settled by a Postgres advisory lock, like the other
loops: two instances visiting together would be two sign-ins for one answer.
Needs the mirror kept current (``MIRROR_SYNC_ENABLED``), since that is where
new tenders are seen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ariba import bcd, portal, service
from app.core.config import Settings
from app.proposals.sharepoint import SharePointProposals

logger = logging.getLogger("hamdaz.ariba.worker")

#: Beside the intake's and the follow-ups' locks, and used by nothing else.
ARIBA_LOCK = 812_404

#: A refused sign-in stops sign-ins for this long. Retrying a wrong password
#: on a timer is how an account gets locked.
PAUSE_AFTER_REFUSAL = timedelta(hours=24)


class AribaWorker:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        sharepoint: SharePointProposals,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._sharepoint = sharepoint
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None and self._settings.ariba_enabled:
            self._task = asyncio.create_task(self._loop())
            logger.info("ariba worker started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task

    async def _loop(self) -> None:
        await asyncio.sleep(30)
        # Zero, so the first pass after a start compares BCD straight away.
        last_bcd = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("ariba tick failed")
            # BCD on its own clock as well: a row edited by hand, or a
            # difference found while writing was off, is corrected without
            # waiting for the next tender. The events already held and one
            # read of the list — never a portal visit.
            if loop.time() - last_bcd >= max(300, self._settings.ariba_bcd_check_seconds):
                try:
                    logger.info("ariba BCD check: %s", await self.check_bcd_now())
                except Exception:  # noqa: BLE001
                    logger.exception("ariba BCD check failed")
                last_bcd = loop.time()
            await asyncio.sleep(max(30, self._settings.ariba_check_seconds))

    async def tick(self, *, force: bool = False) -> str:
        """Look, and visit if it is earned. ``force`` is somebody asking now,
        and skips the wait for new tenders — never the pause after a refusal."""
        settings = self._settings
        if not settings.ariba_configured:
            return "no Ariba login configured"
        async with self._factory() as session:
            got = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": ARIBA_LOCK}
            )
            if not got:
                return "another instance is visiting"
            record = await service.state(session)
            now = service.now_utc()
            found = await service.pending(session, settings, record)
            recheck = None
            if not found.references and not force:
                recheck = await service.needs_recheck(session, settings, record)
            if not found.references and not force and recheck is None:
                result = "nothing new"
                if found.known:
                    # A new row for a tender already read: its BCD is checked
                    # against what we hold, with no visit to the portal.
                    result = await self._check_bcd(session)
                    record.last_result = f"new row for {', '.join(found.known)}; {result}"
                if found.newest and (record.watermark is None or found.newest > record.watermark):
                    # New rows, none of them a tender we lack: move past them.
                    record.watermark = found.newest
                await session.commit()
                return result

            newest = found.newest or recheck or record.watermark or now
            reason = service.refusal(settings, record, newest, now, force=force)
            if reason:
                await session.commit()
                logger.debug("ariba visit deferred: %s", reason)
                return reason

            service.count_visit(record, now)
            try:
                visit = await portal.read_open_events(
                    username=settings.ariba_username,
                    password=settings.ariba_password,
                    session_state=record.session_state,
                    timezone=settings.ariba_timezone,
                )
            except portal.SignInRefusedError as exc:
                record.session_state = None
                record.paused_until = now + PAUSE_AFTER_REFUSAL
                record.last_error = f"sign-in refused: {exc}"
                await session.commit()
                logger.warning("ariba sign-in refused; paused for a day: %s", exc)
                return record.last_error
            except Exception as exc:  # noqa: BLE001 - the last good table stays
                record.last_error = f"{type(exc).__name__}: {exc}"[:600]
                await session.commit()
                logger.warning("ariba visit failed: %s", record.last_error)
                return record.last_error

            summary = await service.keep(session, settings, record, visit, now)
            if found.newest:
                record.watermark = max(found.newest, record.watermark or found.newest)
            summary = f"{summary}; {await self._check_bcd(session)}"
            record.last_result = summary
            record.last_error = None
            record.paused_until = None
            await session.commit()
            logger.info("ariba visit: %s%s", summary, " (signed in)" if visit.signed_in else "")
            return summary

    async def _check_bcd(self, session: AsyncSession) -> str:
        """The BCD comparison, reported rather than raised: the events a visit
        read are worth keeping even when the list cannot be reached."""
        try:
            report = await bcd.check(session, self._settings, self._sharepoint)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ariba BCD check failed: %s", exc)
            return f"BCD check failed: {type(exc).__name__}: {exc}"[:300]
        return report.summary(writing=self._settings.ariba_fix_bcd)

    async def check_bcd_now(self) -> str:
        """The comparison alone, from the events already held — no portal visit."""
        async with self._factory() as session:
            got = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": ARIBA_LOCK}
            )
            if not got:
                return "the reader is busy; try again in a minute"
            summary = await self._check_bcd(session)
            await session.commit()
            return summary
