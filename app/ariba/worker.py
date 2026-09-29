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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ariba import bcd, portal, service
from app.ariba.mailer import AribaMailer
from app.core.config import Settings
from app.proposals.sharepoint import SharePointProposals

logger = logging.getLogger("hamdaz.ariba.worker")

#: Beside the intake's and the follow-ups' locks, and used by nothing else.
ARIBA_LOCK = 812_404



class AribaWorker:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        sharepoint: SharePointProposals,
        mailer: AribaMailer,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._sharepoint = sharepoint
        self._mailer = mailer
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
        and skips the wait for new tenders — never a stop, a failed sign-in's block
        or the daily caps."""
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
            if record.stopped_at is not None:
                # Stopped from the admin page: nothing at all, and the
                # watermark stays put so starting again catches up.
                await session.commit()
                return f"stopped by {record.stopped_by or 'a super admin'}"
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
                    may_sign_in=service.logins_left(settings, record, now) > 0,
                )
            except portal.SignInNotAllowedError:
                # A limit, not a failure: no block, no email. The next day's
                # first visit signs in.
                record.session_state = None
                record.last_result = (
                    f"session ended; today's {settings.ariba_max_logins_per_day} sign-ins are "
                    "used, so the next sign-in is tomorrow"
                )
                await session.commit()
                logger.info("ariba: %s", record.last_result)
                return record.last_result
            except portal.SignInFailedError as exc:
                service.count_login(record, now)
                # Any failed sign-in stops sign-ins until a super admin resumes
                # them — never retried on a timer — and every super admin is told.
                reason = str(exc)[:600]
                record.session_state = None
                record.blocked_at = now
                record.blocked_reason = reason
                record.last_error = f"sign-in failed: {reason}"
                await session.commit()
                logger.warning("ariba sign-in failed; stopped until resumed: %s", reason)
                unsent = await self._mailer.sign_in_failed(session, reason=reason, at=now)
                if unsent:
                    record.last_error = f"{record.last_error} (super admins not emailed: {unsent})"
                await session.commit()
                return record.last_error
            except Exception as exc:  # noqa: BLE001 - the last good table stays
                # The session held, the page did not. Retried at the next
                # visit; the super admins are told once, when failures begin.
                first = record.last_error is None
                record.last_error = f"{type(exc).__name__}: {exc}"[:600]
                await session.commit()
                logger.warning("ariba visit failed: %s", record.last_error)
                if first:
                    unsent = await self._mailer.visit_failed(
                        session, reason=record.last_error, at=now
                    )
                    if unsent:
                        record.last_error = f"{record.last_error} (not emailed: {unsent})"
                        await session.commit()
                return record.last_error

            if visit.signed_in:
                service.count_login(record, now)
            summary = await service.keep(session, settings, record, visit, now)
            if found.newest:
                record.watermark = max(found.newest, record.watermark or found.newest)
            summary = f"{summary}; {await self._check_bcd(session)}"
            record.last_result = summary
            record.last_error = None
            await session.commit()
            logger.info("ariba visit: %s%s", summary, " (signed in)" if visit.signed_in else "")
            return summary

    async def set_stopped(self, stopped: bool, *, by: str) -> str:
        """The admin page's switch. Stopped: no visits, no BCD corrections —
        taken under the same lock, so a visit in flight finishes first."""
        async with self._factory() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ARIBA_LOCK})
            record = await service.state(session)
            if stopped:
                record.stopped_at = service.now_utc()
                record.stopped_by = by
            else:
                record.stopped_at = None
                record.stopped_by = None
            await session.commit()
        logger.warning("ariba reader %s by %s", "stopped" if stopped else "started", by)
        return "stopped: no visits and no BCD corrections" if stopped else "started"

    async def resume(self) -> str:
        """A super admin has looked: sign-ins may happen again."""
        async with self._factory() as session:
            record = await service.state(session)
            was = record.blocked_reason
            record.blocked_at = None
            record.blocked_reason = None
            record.paused_until = None
            record.last_error = None
            await session.commit()
        logger.info("ariba sign-in resumed (was: %s)", was)
        return "sign-in resumed; the next visit will sign in" if was else "sign-in was not stopped"

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
            record = await service.state(session)
            if record.stopped_at is not None:
                await session.commit()
                return f"stopped by {record.stopped_by or 'a super admin'}; BCD not checked"
            summary = await self._check_bcd(session)
            await session.commit()
            return summary
