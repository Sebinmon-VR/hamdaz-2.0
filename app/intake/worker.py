"""The loop that keeps the local copy of the Proposals list current.

Pull the Proposals list into the local copy and recompute who is next. This is
what makes the priority score current instead of computed when somebody asks,
and it is the only thing that touches SharePoint on a clock — reading it, and
publishing the ranking where that is switched on.

The mail half of this worker — watching a mailbox and sorting what arrived
with a model — was removed on 2026-10-14; the reader of the mailbox itself is
kept in ``graph_mail`` for the quote pages.

**Only one instance runs the loop.** On App Service there is usually more than
one, and a Postgres advisory lock settles it: whoever takes it runs, everybody
else sleeps and tries again later, and the lock is released by the commit or
rollback that ends the work, so a crashed instance releases it without anybody
intervening.

The loop is deliberately hard to notice. It logs at debug when nothing
happened, never raises into the event loop, and an error leaves the last good
state in place — a stale mirror beats a broken one.
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
from app.models.assignment import AssignmentPolicy
from app.models.team import Team
from app.proposals import mirror as mirror_service
from app.proposals.mirror import MirrorSync
from app.proposals.sharepoint import SharePointError, SharePointProposals

logger = logging.getLogger("hamdaz.intake.worker")

#: Arbitrary, and constant. A number nobody else in this database uses.
MIRROR_LOCK = 812_401

#: A list subscription this close to expiry is renewed at the next sync.
RENEW_WITHIN = timedelta(days=2)

#: How long a failed loop waits before trying again. Longer than the normal
#: interval on purpose: whatever broke is not usually fixed within the second,
#: and hammering it makes the logs useless.
BACKOFF_SECONDS = 300


async def _take_lock(session: AsyncSession, key: int) -> bool:
    """Try to become the instance that runs this loop.

    Transaction-scoped on purpose. The loop does its work inside the one
    transaction it opens here and ends with a commit, so the lock lasts exactly
    as long as the work and goes away with it — on commit, on rollback, or when
    a crashed instance's connection is dropped.

    The session-scoped variant was used before and leaked: a connection here
    comes from a pool, so after the commit the session hands it back and the
    unlock runs on whichever connection it is given next. The lock stayed on
    the first one, every later attempt saw "another instance holds it", and the
    mirror quietly stopped refreshing.
    """
    got = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key})
    return bool(got)


class Worker:
    """Owns the loop. Started and stopped with the application."""

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
        task = asyncio.create_task(self._mirror_loop())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("mirror worker started")

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
                if not self._mirror_wanted():
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

    def _mirror_wanted(self) -> bool:
        """Whether the mirror should be kept current: ``MIRROR_SYNC_ENABLED``.

        Before 2026-10-14 a switched-on mail intake also kept it running, since
        matching an email meant searching the mirror. The intake's sorting is
        gone, so the environment flag alone decides.
        """
        return bool(self._settings.mirror_sync_enabled)

    async def sync_once(self, *, force: bool = False) -> None:
        """One mirror refresh, then rewrite who is next.

        The recompute follows the sync in the same session on purpose: the
        counts and the ranking derived from them should never be visible in
        disagreement, and the whole thing is two queries once the list is in.

        ``force`` is for the admin route, which is somebody asking explicitly
        and should not be refused because a background flag is off.
        """
        if not force and not self._mirror_wanted():
            return
        async with self._factory() as session:
            if not await _take_lock(session, MIRROR_LOCK):
                logger.debug("another instance holds the mirror lock")
                return
            report = await self._mirror.run(session, self._sharepoint)
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
                "mirror: read=%d changed=%d in %dms",
                report.read, report.changed, report.duration_ms,
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
        the company.

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

    # ── used by the routes ─────────────────────────────────────────────

    @property
    def last_sync(self):
        return self._mirror.last

    @property
    def publisher(self) -> publishing.Publisher:
        return self._publisher
