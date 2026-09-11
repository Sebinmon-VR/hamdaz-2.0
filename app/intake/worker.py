"""The loops that make this live: keep the mirror current, keep watching mail.

Two jobs on two timers, both in-process:

* **mirror** — pull the Proposals list into the local copy and recompute who is
  next. This is what makes the priority score current instead of computed when
  somebody asks, and it is the only thing that touches SharePoint on a clock.
* **mail** — ask Graph what has arrived and take each new message through the
  pipeline.

**Only one instance runs each loop.** On App Service there is usually more than
one, and two copies of the mail loop would read the same inbox and race each
other into the same work. A Postgres advisory lock settles it: whoever takes it
runs, everybody else sleeps and tries again later, and a lock dies with the
connection so a crashed instance releases it without anybody intervening.

Both loops are deliberately hard to notice. They log at debug when nothing
happened, they never raise into the event loop, and an error leaves the last
good state in place — a stale mirror beats a broken one, and a mailbox that
cannot be read is a message on the settings screen rather than a dead app.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analytics import live as live_scores
from app.core.config import Settings
from app.intake import service as intake_service
from app.intake.classifier import Classifier
from app.intake.graph_mail import MailReader
from app.intake.matcher import Matcher
from app.models.intake import IntakeSettings
from app.models.team import Team
from app.proposals.mirror import Embedder, MirrorSync
from app.proposals.sharepoint import SharePointProposals

logger = logging.getLogger("hamdaz.intake.worker")

#: Arbitrary, and constant. Two numbers nobody else in this database uses, so
#: the two loops do not block each other and neither blocks anything else.
MIRROR_LOCK = 812_401
MAIL_LOCK = 812_402

#: How long a failed loop waits before trying again. Longer than the normal
#: interval on purpose: whatever broke — SharePoint, Graph, a model — is not
#: usually fixed within the second, and hammering it makes the logs useless.
BACKOFF_SECONDS = 300


async def _take_lock(session: AsyncSession, key: int) -> bool:
    """Try to become the instance that runs this loop.

    Session-scoped rather than transactional, so it is held for as long as the
    work takes and released when the connection goes — which is what makes a
    crashed instance recover without anybody noticing.
    """
    got = await session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
    return bool(got)


async def _release(session: AsyncSession, key: int) -> None:
    await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


class Worker:
    """Owns the loops. Started and stopped with the application."""

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        sharepoint: SharePointProposals,
        mail: MailReader,
        http,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._sharepoint = sharepoint
        self._mail = mail
        self._http = http
        self._embedder = Embedder(settings)
        self._classifier = Classifier(settings)
        self._matcher = Matcher(settings, self._embedder)
        self._mirror = MirrorSync()
        self._tasks: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._tasks:
            return
        for coro in (self._mirror_loop(), self._mail_loop()):
            task = asyncio.create_task(coro)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        logger.info("intake worker started")

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

    async def _sleep(self, seconds: float) -> bool:
        """Wait, or wake early on shutdown. False means stop."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            return True
        return False

    # ── the mirror ─────────────────────────────────────────────────────

    async def _mirror_loop(self) -> None:
        # A moment before the first run, so start-up is not competing with a
        # list read on a cold connection pool.
        if not await self._sleep(10):
            return
        while not self._stopping.is_set():
            interval = max(30, self._settings.mirror_sync_seconds)
            try:
                await self.sync_once()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("mirror sync failed")
                interval = BACKOFF_SECONDS
            if not await self._sleep(interval):
                return

    async def _mirror_wanted(self) -> bool:
        """Whether the mirror should be kept current at all.

        Either because somebody set the environment flag — which is how the
        live ranking is kept fresh without any mail involved — or because the
        intake is on, which cannot work without it: matching an email means
        searching the mirror.
        """
        if self._settings.mirror_sync_enabled:
            return True
        async with self._factory() as session:
            intake = await intake_service.get_settings(session)
            await session.commit()
            return bool(intake.enabled)

    async def sync_once(self, *, embed: bool = True, force: bool = False) -> None:
        """One mirror refresh, then rewrite who is next.

        The recompute follows the sync in the same session on purpose: the
        counts and the ranking derived from them should never be visible in
        disagreement, and the whole thing is two queries once the list is in.

        ``force`` is for the admin route, which is somebody asking explicitly
        and should not be refused because a background flag is off.
        """
        if not force and not await self._mirror_wanted():
            return
        async with self._factory() as session:
            if not await _take_lock(session, MIRROR_LOCK):
                logger.debug("another instance holds the mirror lock")
                return
            try:
                report = await self._mirror.run(
                    session, self._sharepoint, self._embedder, embed=embed
                )
                if report.error:
                    logger.warning("mirror sync: %s", report.error)
                    await session.commit()
                    return
                await self._recompute_all(session, reason="mirror")
                await session.commit()
                logger.debug(
                    "mirror: read=%d changed=%d embedded=%d in %dms",
                    report.read, report.changed, report.embedded, report.duration_ms,
                )
            finally:
                await _release(session, MIRROR_LOCK)

    async def _recompute_all(self, session: AsyncSession, *, reason: str) -> None:
        """The organisation-wide ranking, and one per team that has a policy.

        Per team as well as overall because they answer different questions —
        the least loaded person in presales is not the least loaded person in
        the company, and the intake assigns from a team's ranking.
        """
        await live_scores.recompute(session, team=None, reason=reason)
        teams = (
            await session.scalars(select(Team).where(Team.archived_at.is_(None)))
        ).all()
        for team in teams:
            await live_scores.recompute(session, team=team, reason=reason)

    # ── the mail ───────────────────────────────────────────────────────

    async def _mail_loop(self) -> None:
        if not await self._sleep(20):
            return
        while not self._stopping.is_set():
            interval = 60
            try:
                interval = await self.poll_once()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("mail poll failed")
                interval = BACKOFF_SECONDS
            if not await self._sleep(interval):
                return

    async def poll_once(self) -> int:
        """Ask what has arrived, record it, and work through it.

        Returns how long to wait before asking again, which is the configured
        interval unless the intake is switched off — in which case it is a lazy
        check for somebody having turned it on.
        """
        async with self._factory() as session:
            intake = await intake_service.get_settings(session)
            if not intake.enabled or not intake.mailbox:
                await session.commit()
                return 120
            interval = max(15, intake.poll_seconds)

            if not await _take_lock(session, MAIL_LOCK):
                logger.debug("another instance holds the mail lock")
                await session.commit()
                return interval
            try:
                await self._drain(session, intake)
                await session.commit()
            finally:
                await _release(session, MAIL_LOCK)
            return interval

    async def _drain(self, session: AsyncSession, intake: IntakeSettings) -> None:
        try:
            messages, cursor = await self._mail.delta(
                intake.mailbox,
                delta_link=intake.delta_link,
                since=intake.watch_from,
            )
        except Exception as exc:  # noqa: BLE001 - shown on the settings screen
            intake.last_error = f"{type(exc).__name__}: {exc}"
            intake.last_poll_at = datetime.now(UTC)
            await session.flush()
            return

        fresh = 0
        for raw in messages:
            if raw.get("isDraft"):
                continue
            row = await intake_service.record(session, raw)
            if row is None:
                continue
            fresh += 1
            # The delta projection has no body. Fetching it only for messages
            # that are new, and only after the sender filter would keep them,
            # is what stops a busy inbox costing a request per message.
            if row.body is None or len(row.body) < 40:
                try:
                    row.body = await self._mail.body_of(
                        intake.mailbox, row.graph_message_id
                    )
                except Exception:  # noqa: BLE001 - classify on the preview
                    logger.debug("could not fetch body for %s", row.graph_message_id)
        await session.flush()

        if cursor:
            intake.delta_link = cursor
        intake.last_poll_at = datetime.now(UTC)
        intake.last_error = None
        await session.flush()

        for row in await intake_service.pending(session, limit=20):
            await intake_service.process(
                session, row,
                settings=self._settings,
                intake=intake,
                classifier=self._classifier,
                matcher=self._matcher,
                sharepoint=self._sharepoint,
                http=self._http,
            )
            await session.flush()
        if fresh:
            logger.info("intake: %d new message(s)", fresh)

    # ── used by the routes ─────────────────────────────────────────────

    async def process_message_id(self, message_id: str) -> None:
        """Handle one message Graph told us about, now rather than at the poll."""
        async with self._factory() as session:
            intake = await intake_service.get_settings(session)
            if not intake.enabled or not intake.mailbox:
                return
            try:
                raw = await self._mail.message(intake.mailbox, message_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not read message %s: %s", message_id, exc)
                return
            row = await intake_service.record(session, raw)
            if row is None:
                await session.commit()
                return
            await intake_service.process(
                session, row,
                settings=self._settings,
                intake=intake,
                classifier=self._classifier,
                matcher=self._matcher,
                sharepoint=self._sharepoint,
                http=self._http,
            )
            await session.commit()

    @property
    def last_sync(self):
        return self._mirror.last

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    @property
    def matcher(self) -> Matcher:
        return self._matcher

    @property
    def classifier(self) -> Classifier:
        return self._classifier
