"""Background tasks.

Every task records a ``job_runs`` row — start, finish, duration, error — because a job the
developer panel cannot see is a job nobody can debug. Root cause #4 was three ``while True``
threads inside the web process with no visibility and no retries; this is the replacement.

Nothing here writes to SharePoint (C2).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.core.compat import run as run_async
from app.core.db import session_scope
from app.core.logging import bind_correlation_id, get_logger
from app.models.platform import JobRun, JobStatus
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def _run_tracked[T](
    task_name: str,
    body: Callable[[], Awaitable[T]],
    *,
    args: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> T:
    """Run a task body, recording the attempt either way.

    The ``job_runs`` row is written in its own session, separate from whatever the body does,
    so a failed task still leaves a visible record instead of rolling its own audit away.
    """
    correlation_id = bind_correlation_id(task_id)
    started_at = datetime.now(UTC)
    started = time.perf_counter()

    run_id = uuid.uuid4()
    async with session_scope() as session:
        session.add(
            JobRun(
                id=run_id,
                task_name=task_name,
                task_id=task_id,
                args=args,
                status=JobStatus.RUNNING,
                started_at=started_at,
                correlation_id=correlation_id,
                created_at=started_at,
            )
        )

    try:
        result = await body()
    except Exception as exc:
        duration = int((time.perf_counter() - started) * 1000)
        logger.exception("job.failed", task=task_name, error=str(exc))
        async with session_scope() as session:
            run = await session.get(JobRun, run_id)
            if run is not None:
                run.status = JobStatus.FAILED
                run.finished_at = datetime.now(UTC)
                run.duration_ms = duration
                run.error = f"{type(exc).__name__}: {exc}"
        raise

    duration = int((time.perf_counter() - started) * 1000)
    async with session_scope() as session:
        run = await session.get(JobRun, run_id)
        if run is not None:
            run.status = JobStatus.SUCCESS
            run.finished_at = datetime.now(UTC)
            run.duration_ms = duration

    logger.info("job.succeeded", task=task_name, duration_ms=duration)
    return result


# ── SharePoint delta sync (read-only) ──────────────────────────────────


async def _sync_sharepoint_proposals() -> dict[str, Any]:
    """Pull proposal changes from SharePoint into Postgres.

    **Read only.** The connector has no write methods, so this can only ever ingest. The
    delta cursor is persisted to ``connector_status``, not held in a module global — that is
    what makes it safe to run from more than one worker.
    """
    from app.services.sharepoint_sync import sync_proposals

    async with session_scope() as session:
        return await sync_proposals(session)


@celery_app.task(name="app.workers.tasks.sync_sharepoint_proposals", bind=True, max_retries=3)
def sync_sharepoint_proposals(self: Any) -> dict[str, Any]:
    return run_async(
        _run_tracked(
            "app.workers.tasks.sync_sharepoint_proposals",
            _sync_sharepoint_proposals,
            task_id=getattr(self.request, "id", None),
        )
    )


# ── label maintenance ──────────────────────────────────────────────────


async def _expire_labels() -> dict[str, Any]:
    """Housekeeping for expired label assignments.

    Reads already filter on expiry, so this only reclaims rows — a label never lingers in
    effect just because this has not run.
    """
    from app.services.label_service import purge_expired

    async with session_scope() as session:
        removed = await purge_expired(session)
    return {"removed": removed}


@celery_app.task(name="app.workers.tasks.expire_labels", bind=True)
def expire_labels(self: Any) -> dict[str, Any]:
    return run_async(
        _run_tracked(
            "app.workers.tasks.expire_labels",
            _expire_labels,
            task_id=getattr(self.request, "id", None),
        )
    )


# ── proposal escalation ────────────────────────────────────────────────


async def _escalate_proposals() -> dict[str, Any]:
    """Run the ``proposal.escalate`` rules over open proposals nearing their deadline."""
    from sqlalchemy import select

    from app.models.proposals import OPEN_STATUS_VALUES, Proposal
    from app.services import rules_service

    escalated = 0
    now = datetime.now(UTC)

    async with session_scope() as session:
        proposals = (
            await session.scalars(
                select(Proposal)
                .where(Proposal.status.in_(OPEN_STATUS_VALUES), Proposal.bcd.is_not(None))
                .limit(500)
            )
        ).all()

        for proposal in proposals:
            bcd = proposal.bcd
            if bcd is None:
                continue
            if bcd.tzinfo is None:
                bcd = bcd.replace(tzinfo=UTC)

            days_to_bcd = (bcd - now).total_seconds() / 86400.0
            days_since_assigned = (
                (now - proposal.assigned_at.replace(tzinfo=UTC)).total_seconds() / 86400.0
                if proposal.assigned_at
                else 0.0
            )

            decision, _ = await rules_service.evaluate(
                session,
                decision_point="proposal.escalate",
                facts={
                    "proposal.status": proposal.status.value,
                    "proposal.days_to_bcd": days_to_bcd,
                    "proposal.days_since_assigned": days_since_assigned,
                    "proposal.value": float(proposal.estimated_value or 0),
                },
                team_id=proposal.team_id,
                entity_type="proposal",
                entity_id=str(proposal.id),
            )

            if decision.matched:
                escalated += 1
                for action in decision.actions_of_type("raise_priority"):
                    proposal.priority_score += int(action.get("by", 1))

    return {"checked": len(proposals), "escalated": escalated}


@celery_app.task(name="app.workers.tasks.escalate_proposals", bind=True)
def escalate_proposals(self: Any) -> dict[str, Any]:
    return run_async(
        _run_tracked(
            "app.workers.tasks.escalate_proposals",
            _escalate_proposals,
            task_id=getattr(self.request, "id", None),
        )
    )
