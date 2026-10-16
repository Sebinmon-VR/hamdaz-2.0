"""The always-on half of agents: a task lands, the agent starts.

Every minute, while AGENT_TRIGGERS_ENABLED is on: bring every Nova somebody
added up to date (every ten minutes — none is made here), start every switched-on agent whose trigger is
"A task is assigned to me" on any task newly in its owner's name in the
Proposals mirror, and pick up runs a restart cut off while they waited for
documents or for a yes to send. And every three minutes, while
AGENT_WATCH_REPLIES is on, look for suppliers' replies to the RFQs agents sent
(``app.agents.rfqs``).

"Newly in their name" is worked out against what the agent has already seen
(``trigger_state.seen``), not against creation dates — so a task reassigned
to somebody starts their agent as surely as a new one. The first look at an
agent only records what is already there: switching an agent on does not set
it loose on every open task.

One instance at a time, settled by an advisory lock, as for the other loops;
"no run of this agent on this task yet" is the second guard.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import engine, nova, rfqs
from app.core.config import Settings
from app.models.agent import AgentRun, UserAgent
from app.models.proposal_index import ProposalIndexItem
from app.models.user import User

logger = logging.getLogger("hamdaz.agents.worker")

#: Beside the others (812_401 – 812_407), used by nothing else.
AGENT_LOCK = 812_408
POLL_SECONDS = 60
BACKOFF_SECONDS = 300
PROVISION_SECONDS = 600
#: How often inboxes are looked at for replies to agents' RFQs.
WATCH_SECONDS = 180
#: New tasks one agent starts on in one pass; the rest wait for the next.
MAX_STARTS_PER_PASS = 3
#: Runs one person may have going at once, started on their own.
MAX_LIVE_PER_OWNER = 5
#: A run waiting for a yes to send gives up after this, as the runner would.
SEND_WAIT = timedelta(days=3)


class AgentWorker:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        deps: Callable[[], engine.Deps],
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._deps = deps
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._provisioned = 0.0
        self._watched = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("agent worker started")

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
        # The mirror's first sync goes first.
        if not await self._sleep(45):
            return
        while not self._stopping.is_set():
            wait = POLL_SECONDS
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("agent worker pass failed")
                wait = BACKOFF_SECONDS
            if not await self._sleep(wait):
                return

    async def run_once(self) -> None:
        triggers = self._settings.agent_triggers_enabled
        watching = self._settings.agent_watch_replies
        if not triggers and not watching:
            return
        replied: list[uuid.UUID] = []
        started: list[uuid.UUID] = []
        picked_up: list[uuid.UUID] = []
        async with self._factory() as session:
            got = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": AGENT_LOCK})
            if not got:
                await session.commit()
                return
            if triggers:
                if time.monotonic() - self._provisioned >= PROVISION_SECONDS:
                    await nova.refresh_all(session, self._settings)
                    self._provisioned = time.monotonic()
                started = await self._triggers(session)
                picked_up = await self._cut_off(session)
            if watching and time.monotonic() - self._watched >= WATCH_SECONDS:
                self._watched = time.monotonic()
                replied = await rfqs.watch(session, self._deps())
            await session.commit()
        # Started once their rows are committed: a run opens its own session.
        deps = self._deps()
        for run_id in started:
            engine.start(deps, run_id, [])
        for run_id in picked_up:
            await engine.answer_cut_off(deps, run_id)
        if replied:
            logger.info("%d supplier repl(ies) to agents' RFQs", len(replied))
            await rfqs.tell_replied(deps, replied)
        if started:
            logger.info("agents started on %d new task(s)", len(started))

    async def _triggers(self, session: AsyncSession) -> list[uuid.UUID]:
        started: list[uuid.UUID] = []
        agents = (await session.scalars(select(UserAgent).where(UserAgent.always_on.is_(True)))).all()
        today = date.today()
        for agent in agents:
            steps = agent.steps or []
            if not steps or steps[0].get("kind") != "start_new_task":
                continue
            owner = await session.get(User, agent.owner_id)
            if owner is None or not owner.is_active or not owner.email:
                continue
            rows = (await session.scalars(
                select(ProposalIndexItem)
                .where(func.lower(ProposalIndexItem.assigned_email) == owner.email.lower())
                .where(ProposalIndexItem.is_open.is_(True), ProposalIndexItem.deleted.is_(False))
                .order_by(ProposalIndexItem.sp_created_at.asc().nulls_last())
            )).all()
            open_ids = [r.item_id for r in rows]
            state = dict(agent.trigger_state or {})
            if "seen" not in state:
                # The first look: what is already there is not new.
                agent.trigger_state = {"seen": open_ids, "since": datetime.now(UTC).isoformat(timespec="seconds")}
                continue
            seen = set(state.get("seen") or [])
            contains = str((steps[0].get("config") or {}).get("contains") or "").strip().casefold()
            live = await session.scalar(
                select(func.count()).select_from(AgentRun)
                .where(AgentRun.owner_id == owner.id, AgentRun.status.in_(("running", "waiting")))
            ) or 0
            took: list[str] = []
            for row in [r for r in rows if r.item_id not in seen][:MAX_STARTS_PER_PASS]:
                if live >= MAX_LIVE_PER_OWNER:
                    break  # Not marked seen: it starts once a run finishes.
                took.append(row.item_id)
                if contains and contains not in (row.title or "").casefold():
                    continue
                closes = row.bid_closing_date or row.deadline or row.due_date
                if closes is not None and closes < today:
                    continue
                already = await session.scalar(
                    select(AgentRun.id).where(AgentRun.agent_id == agent.id, AgentRun.task_id == row.item_id).limit(1)
                )
                if already is not None:
                    continue
                run = AgentRun(
                    agent_id=agent.id, owner_id=owner.id, task_id=row.item_id, task_title=row.title,
                    status="running", started_by="trigger", heartbeat_at=datetime.now(UTC),
                    plan={"name": agent.name, "instructions": agent.instructions, "steps": agent.steps},
                    results=[],
                )
                session.add(run)
                await session.flush()
                started.append(run.id)
                live += 1
            # Kept to what is still open in their name, so the list does not grow forever.
            still_open = set(open_ids)
            agent.trigger_state = {**state, "seen": [i for i in state.get("seen") or [] if i in still_open] + took}
        return started

    async def _cut_off(self, session: AsyncSession) -> list[uuid.UUID]:
        """Runs a restart cut off: the ones that can carry on, to hand to ``answer_cut_off``."""
        picked: list[uuid.UUID] = []
        rows = (await session.scalars(select(AgentRun).where(AgentRun.status.in_(("running", "waiting"))))).all()
        now = datetime.now(UTC)
        for run in rows:
            if not engine.is_cut_off(run):
                continue
            question = run.question or {}
            if not engine.resumable(run):
                engine.settle_orphan(run)
            elif question.get("mode") == "docs" or isinstance(question.get("answer"), dict):
                picked.append(run.id)
            elif now - (engine.parse_time(question.get("asked_at")) or now) > SEND_WAIT:
                run.status, run.question, run.finished_at = "stopped", None, now
                run.summary = f"{(run.plan or {}).get('name') or 'Your agent'} stopped: no answer came within 3 days, so nothing was sent."
        return picked
