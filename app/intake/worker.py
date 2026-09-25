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
runs, everybody else sleeps and tries again later, and the lock is released by
the commit or rollback that ends the work, so a crashed instance releases it
without anybody intervening.

Both loops are deliberately hard to notice. They log at debug when nothing
happened, they never raise into the event loop, and an error leaves the last
good state in place — a stale mirror beats a broken one, and a mailbox that
cannot be read is a message on the settings screen rather than a dead app.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analytics import live as live_scores
from app.analytics import publisher as publishing
from app.core.config import Settings
from app.intake import service as intake_service
from app.intake.classifier import Classifier
from app.intake.graph_mail import MailReader
from app.intake.matcher import Matcher
from app.models.assignment import AssignmentPolicy
from app.models.intake import IntakeSettings
from app.models.team import Team
from app.proposals import mirror as mirror_service
from app.proposals.mirror import Embedder, MirrorSync
from app.proposals.sharepoint import SharePointError, SharePointProposals

logger = logging.getLogger("hamdaz.intake.worker")

#: Arbitrary, and constant. Two numbers nobody else in this database uses, so
#: the two loops do not block each other and neither blocks anything else.
MIRROR_LOCK = 812_401
MAIL_LOCK = 812_402

#: A list subscription this close to expiry is renewed at the next sync.
RENEW_WITHIN = timedelta(days=2)

#: How long a failed loop waits before trying again. Longer than the normal
#: interval on purpose: whatever broke — SharePoint, Graph, a model — is not
#: usually fixed within the second, and hammering it makes the logs useless.
BACKOFF_SECONDS = 300


async def _take_lock(session: AsyncSession, key: int) -> bool:
    """Try to become the instance that runs this loop.

    Transaction-scoped on purpose. Each loop does its work inside the one
    transaction it opens here and ends with a commit, so the lock lasts exactly
    as long as the work and goes away with it — on commit, on rollback, or when
    a crashed instance's connection is dropped.

    The session-scoped variant was used before and leaked: a connection here
    comes from a pool, so after the commit the session hands it back and the
    unlock runs on whichever connection it is given next. The lock stayed on
    the first one, every later attempt saw "another instance holds it", and the
    mirror quietly stopped refreshing. An error was worse still, because the
    unlock then ran inside the aborted transaction and buried the real failure.
    """
    got = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key})
    return bool(got)


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
        self._publisher = publishing.Publisher(settings, sharepoint)
        self._tasks: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()
        #: Set by the webhook when SharePoint reports the list changed, so the
        #: mirror loop wakes now rather than at the end of its interval.
        self._kick = asyncio.Event()
        self._kicked = False
        self._kick_seen = False
        #: The list's newest Modified stamp as of the last sync, so the watch
        #: can tell "something changed" from "nothing has".
        self._seen_modified: str | None = None

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

    async def _sleep(self, seconds: float, *, wake: asyncio.Event | None = None) -> bool:
        """Wait, or wake early on shutdown — or on ``wake``. False means stop."""
        waiters = [asyncio.ensure_future(self._stopping.wait())]
        if wake is not None:
            waiters.append(asyncio.ensure_future(wake.wait()))
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        if wake is not None:
            wake.clear()
        return not self._stopping.is_set()

    def kick(self) -> None:
        """Sync at the next opportunity rather than at the next tick.

        Called by the webhook. It sets a flag rather than running the sync,
        because Graph gives a notification thirty seconds and a list read can
        take ten; and because five notifications for one edit should cost one
        sync, which is what a flag does and a call does not.
        """
        self._kick_seen = True
        self._kick.set()

    # ── the mirror ─────────────────────────────────────────────────────

    async def _mirror_loop(self) -> None:
        """Keep the mirror current: on a timer, and the moment the list moves.

        Between full syncs the loop asks SharePoint one small question every
        few seconds — when did anything in the list last change — and syncs
        as soon as the answer is new. That is what makes a task assigned in
        SharePoint reach the ranking, and the published list, in seconds. The
        full sync keeps its own timer because a deletion moves nothing, and
        the webhook, where it is reachable, simply wakes this loop early.
        """
        # A moment before the first run, so start-up is not competing with a
        # list read on a cold connection pool.
        if not await self._sleep(10):
            return
        last_full = 0.0
        while not self._stopping.is_set():
            wait = max(3, self._settings.mirror_watch_seconds)
            try:
                if not await self._mirror_wanted():
                    wait = 60
                else:
                    full_due = (
                        time.monotonic() - last_full
                        >= max(30, self._settings.mirror_sync_seconds)
                    )
                    if full_due or self._kicked or await self._list_moved():
                        self._kicked = False
                        await self.sync_once()
                        last_full = time.monotonic()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("mirror sync failed")
                wait = BACKOFF_SECONDS
            if not await self._sleep(wait, wake=self._kick):
                return
            if self._kick_seen:
                self._kick_seen = False
                self._kicked = True

    async def _list_moved(self) -> bool:
        """Whether anything in the list changed since the last sync read it."""
        try:
            stamp = await self._sharepoint.newest_modified()
        except SharePointError as exc:
            logger.debug("could not read the list's newest change: %s", exc)
            return False
        if stamp is None or stamp == self._seen_modified:
            return False
        logger.info("proposals list moved at %s; syncing", stamp)
        return True

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
            report = await self._mirror.run(
                session, self._sharepoint, self._embedder, embed=embed
            )
            if report.error:
                logger.warning("mirror sync: %s", report.error)
                await session.commit()
                return
            self._seen_modified = report.newest_modified
            await self._recompute_all(session, reason="mirror")
            # The list is written before the commit on purpose: a publish
            # failure is a warning on the report, never an exception, so the
            # ranking still lands here even when the list is unreachable.
            await publishing.publish_live(session, self._publisher, reason="mirror")
            await self._renew_subscription(session)
            await session.commit()
            logger.debug(
                "mirror: read=%d changed=%d embedded=%d in %dms",
                report.read, report.changed, report.embedded, report.duration_ms,
            )

    async def _renew_subscription(self, session: AsyncSession) -> None:
        """Push the list subscription's expiry out before Graph drops it."""
        state = await mirror_service.state(session)
        if not state.subscription_id or state.subscription_expires_at is None:
            return
        if state.subscription_expires_at - datetime.now(UTC) > RENEW_WITHIN:
            return
        try:
            renewed = await self._sharepoint.renew_subscription(state.subscription_id)
        except SharePointError as exc:
            logger.warning("list subscription not renewed: %s", exc)
            return
        expires = renewed.get("expirationDateTime")
        if expires:
            state.subscription_expires_at = datetime.fromisoformat(
                str(expires).replace("Z", "+00:00")
            )

    async def _recompute_all(self, session: AsyncSession, *, reason: str) -> None:
        """The organisation-wide ranking, and one per team that has a policy.

        Per team as well as overall because they answer different questions —
        the least loaded person in presales is not the least loaded person in
        the company, and the intake assigns from a team's ranking.

        Only teams with an enabled assignment policy of their own: that is the
        rule for distributing work at all, and a ranking for a team that does
        not was several seconds a cycle spent on an answer nobody reads.
        """
        counted = await live_scores.counts(session)
        await live_scores.recompute(session, team=None, reason=reason, counted=counted)
        teams = (
            await session.scalars(
                select(Team)
                .join(AssignmentPolicy, AssignmentPolicy.team_id == Team.id)
                .where(AssignmentPolicy.enabled.is_(True), Team.archived_at.is_(None))
            )
        ).all()
        for team in teams:
            await live_scores.recompute(session, team=team, reason=reason, counted=counted)

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
            await self._drain(session, intake)
            await session.commit()
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
            row = await intake_service.record(session, raw, not_before=intake.watch_from)
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
            row = await intake_service.record(session, raw, not_before=intake.watch_from)
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
    def publisher(self) -> publishing.Publisher:
        return self._publisher

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    @property
    def matcher(self) -> Matcher:
        return self._matcher

    @property
    def classifier(self) -> Classifier:
        return self._classifier
