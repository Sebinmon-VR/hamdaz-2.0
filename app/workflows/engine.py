"""Walking a run through its steps, stopping wherever a step has to wait.

``advance`` is the whole engine, and it is re-entrant by design. It runs the
current step; a step that finishes writes its output into the context and the
next one runs; a step that has to wait — on the person or on the world — sets
the run's status and returns. Whatever wakes the run later (an answer, the
worker's tick) simply calls ``advance`` again, and the same step runs again
with more to go on. So a step handler is written to be called repeatedly until
it is done, and the whole state of a run is the row: the index, the context,
what it is waiting for.

Every side effect a step has on the world — a mail, a Zoho estimate, a
SharePoint attachment — is behind a switch on ``WorkflowSettings``, and the
handlers record what they *would* have done when the switch is off. That is
what lets a flow be run end to end on a fresh deployment and read before
anything leaves the building.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workflow import (
    RunEventKind,
    RunStatus,
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowSettings,
)
from app.workflows.templating import condition_holds

logger = logging.getLogger("hamdaz.workflows")


class WorkflowError(Exception):
    """Something the caller did wrong; the message is written for a person."""


@dataclass(slots=True)
class Services:
    """Everything a step may need, gathered once from the app's state.

    Any of them may be ``None`` in a test or on a deployment without that
    integration; a step that needs one it does not have fails with a sentence
    saying which.
    """

    settings: Any
    sharepoint: Any = None
    mail: Any = None
    zoho: Any = None
    extractor: Any = None
    llm: Any = None
    executor: Any = None
    #: The assistant's model key, for agent steps. Read from its settings so
    #: the two use the same model and the same prices.
    model_key: str = ""


@dataclass(slots=True)
class Done:
    output: Any = None
    #: A short line for the event log — "3 files", "sent to 4".
    note: str | None = None


@dataclass(slots=True)
class Wait:
    #: ``user`` or ``event``.
    on: str
    #: What the person is shown, for a ``user`` wait.
    pending: dict[str, Any] | None = None
    wake_at: datetime | None = None
    deadline_at: datetime | None = None
    note: str | None = None


@dataclass(slots=True)
class Fail:
    message: str


Outcome = Done | Wait | Fail


@dataclass(slots=True)
class StepContext:
    session: AsyncSession
    run: WorkflowRun
    step: dict[str, Any]
    config: dict[str, Any]
    settings: WorkflowSettings
    services: Services

    @property
    def ctx(self) -> dict[str, Any]:
        return self.run.context

    @property
    def key(self) -> str:
        return str(self.step.get("key"))

    def answer(self) -> dict[str, Any] | None:
        """What the person answered on this step, if they have."""
        return (self.ctx.get("_answers") or {}).get(self.key)

    def waited_since(self) -> datetime:
        """When this step first started waiting, so a deadline is measured from then."""
        waits = self.ctx.setdefault("_waits", {})
        stamp = waits.get(self.key)
        if stamp:
            return datetime.fromisoformat(stamp)
        now = datetime.now(UTC)
        waits[self.key] = now.isoformat()
        return now

    async def event(
        self, kind: str, payload: dict[str, Any] | None = None, *, by: uuid.UUID | None = None
    ) -> None:
        await add_event(self.session, self.run, kind, step_key=self.key, payload=payload, by=by)


# ── events ─────────────────────────────────────────────────────────────


async def add_event(
    session: AsyncSession,
    run: WorkflowRun,
    kind: str,
    *,
    step_key: str | None = None,
    payload: dict[str, Any] | None = None,
    by: uuid.UUID | None = None,
) -> WorkflowRunEvent:
    seq = await session.scalar(
        select(func.coalesce(func.max(WorkflowRunEvent.seq), 0)).where(
            WorkflowRunEvent.run_id == run.id
        )
    )
    event = WorkflowRunEvent(
        run_id=run.id, seq=int(seq or 0) + 1, kind=kind, step_key=step_key,
        payload=payload, by_user_id=by,
    )
    session.add(event)
    await session.flush()
    return event


def _touch(run: WorkflowRun) -> None:
    """JSONB columns are only written back when SQLAlchemy sees a new object."""
    run.context = dict(run.context or {})


# ── the walk ───────────────────────────────────────────────────────────


async def advance(
    session: AsyncSession,
    run: WorkflowRun,
    settings: WorkflowSettings,
    services: Services,
    *,
    by: uuid.UUID | None = None,
) -> WorkflowRun:
    """Run steps until one waits, one fails, or there are none left.

    Safe to call on a run in any open state. On a run that is not open it
    does nothing, so a stale wake-up cannot restart a cancelled run.
    """
    from app.workflows.steps import HANDLERS  # circular at import time, not at call time

    if not run.is_open:
        return run

    run.status = RunStatus.RUNNING
    run.pending = None
    run.wake_at = None

    while True:
        step = run.current_step
        if step is None:
            run.status = RunStatus.COMPLETED
            run.finished_at = datetime.now(UTC)
            run.deadline_at = None
            await add_event(session, run, RunEventKind.COMPLETED)
            await session.flush()
            return run

        key = str(step.get("key"))
        if not condition_holds(step.get("when"), run.context):
            await add_event(session, run, RunEventKind.STEP_SKIPPED, step_key=key)
            run.step_index += 1
            continue

        handler = HANDLERS.get(str(step.get("kind")))
        context = StepContext(
            session=session, run=run, step=step, config=dict(step.get("config") or {}),
            settings=settings, services=services,
        )
        if handler is None:
            return await _fail(session, run, key, f"No block called {step.get('kind')!r}.")

        started = (run.context.get("_started") or {}).get(key) is not None
        if not started:
            run.context.setdefault("_started", {})[key] = datetime.now(UTC).isoformat()
            _touch(run)
            await add_event(session, run, RunEventKind.STEP_STARTED, step_key=key)

        try:
            outcome = await handler(context)
        except Exception as exc:  # noqa: BLE001 - a run must record, never raise
            logger.exception("workflow run %s step %s crashed", run.tag, key)
            return await _fail(session, run, key, f"{type(exc).__name__}: {exc}")

        if isinstance(outcome, Fail):
            return await _fail(session, run, key, outcome.message)

        if isinstance(outcome, Wait):
            run.status = RunStatus.WAITING_USER if outcome.on == "user" else RunStatus.WAITING_EVENT
            run.pending = outcome.pending
            run.wake_at = outcome.wake_at
            run.deadline_at = outcome.deadline_at
            _touch(run)
            await add_event(
                session, run, RunEventKind.WAITING, step_key=key,
                payload={"on": outcome.on, "note": outcome.note,
                         "wake_at": outcome.wake_at.isoformat() if outcome.wake_at else None},
            )
            await session.flush()
            return run

        save_as = str(context.config.get("save_as") or "").strip()
        if save_as:
            run.context[save_as] = outcome.output
        _touch(run)
        await add_event(
            session, run, RunEventKind.STEP_COMPLETED, step_key=key,
            payload={"note": outcome.note, "saved_as": save_as or None}, by=by,
        )
        run.step_index += 1
        run.pending = None
        run.wake_at = None
        run.deadline_at = None
        await session.flush()
        by = None  # only the first step after an answer is "by" that person


async def _fail(session: AsyncSession, run: WorkflowRun, key: str, message: str) -> WorkflowRun:
    run.status = RunStatus.FAILED
    run.error = message
    run.pending = None
    run.wake_at = None
    run.finished_at = datetime.now(UTC)
    # What the step learnt before it failed is kept — a retry starts from it.
    _touch(run)
    await add_event(session, run, RunEventKind.ERROR, step_key=key, payload={"message": message})
    await session.flush()
    return run
