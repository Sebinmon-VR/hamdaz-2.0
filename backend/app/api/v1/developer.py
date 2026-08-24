"""Developer panel API (§6).

Two things the legacy system cannot do at all: show what is running right now, and explain
why the system made a decision. The first is the job and connector screens; the second is the
rule inspector, reading ``rule_evaluations``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from app.api.deps import CurrentPrincipal, DbDep, SettingsDep, require
from app.core.rbac import Scope
from app.models.platform import (
    AuditLog,
    ConnectorStatus,
    EmailOutbox,
    FeatureFlag,
    JobRun,
    JobStatus,
)
from app.models.rules import RuleEvaluation
from app.schemas.common import Message, Page, Pagination, pagination
from app.services import audit_service

router = APIRouter(
    prefix="/developer",
    tags=["developer"],
    dependencies=[Depends(require("dev.panel.view", Scope.ALL))],
)

PaginationDep = Annotated[Pagination, Depends(pagination)]


# ── overview ───────────────────────────────────────────────────────────


class SystemOverview(BaseModel):
    environment: str
    #: Constraint status, surfaced where a developer will actually look at it.
    sharepoint_mode: str
    sharepoint_sandbox_site: str
    outbound_email_enabled: bool
    jobs_last_hour: int
    jobs_failed_last_hour: int
    rule_evaluations_last_hour: int
    audit_entries_last_hour: int
    captured_emails: int


@router.get("/overview", response_model=SystemOverview)
async def overview(session: DbDep, settings: SettingsDep) -> SystemOverview:
    since = datetime.now(UTC) - timedelta(hours=1)

    async def _count(model: Any, *where: Any) -> int:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    return SystemOverview(
        environment=settings.environment.value,
        sharepoint_mode=(
            "read_write_sandbox"
            if settings.sharepoint_sandbox_writes_enabled
            else "read_only"
        ),
        sharepoint_sandbox_site=settings.sharepoint_sandbox_site_path,
        outbound_email_enabled=settings.outbound_email_enabled,
        jobs_last_hour=await _count(JobRun, JobRun.created_at >= since),
        jobs_failed_last_hour=await _count(
            JobRun, JobRun.created_at >= since, JobRun.status == JobStatus.FAILED
        ),
        rule_evaluations_last_hour=await _count(
            RuleEvaluation, RuleEvaluation.created_at >= since
        ),
        audit_entries_last_hour=await _count(AuditLog, AuditLog.created_at >= since),
        captured_emails=await _count(EmailOutbox, EmailOutbox.status == "captured"),
    )


# ── jobs ───────────────────────────────────────────────────────────────


class JobRunOut(BaseModel):
    id: str
    task_name: str
    task_id: str | None = None
    status: str
    args: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    error: str | None = None
    retries: int
    correlation_id: str | None = None


@router.get(
    "/jobs",
    response_model=Page[JobRunOut],
    dependencies=[Depends(require("dev.jobs.manage", Scope.ALL))],
)
async def list_jobs(
    session: DbDep,
    page: PaginationDep,
    task_name: Annotated[str | None, Query()] = None,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
) -> Page[JobRunOut]:
    query = select(JobRun)
    if task_name:
        query = query.where(JobRun.task_name == task_name)
    if job_status:
        query = query.where(JobRun.status == job_status)

    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (
        await session.scalars(
            query.order_by(desc(JobRun.created_at)).limit(page.limit).offset(page.offset)
        )
    ).all()

    return Page.of(
        [
            JobRunOut(
                id=str(r.id),
                task_name=r.task_name,
                task_id=r.task_id,
                status=r.status.value,
                args=r.args,
                started_at=r.started_at.isoformat() if r.started_at else None,
                finished_at=r.finished_at.isoformat() if r.finished_at else None,
                duration_ms=r.duration_ms,
                error=r.error,
                retries=r.retries,
                correlation_id=r.correlation_id,
            )
            for r in rows
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


class TaskSummary(BaseModel):
    task_name: str
    runs: int
    failures: int
    avg_duration_ms: int | None = None
    last_run: str | None = None


@router.get(
    "/jobs/summary",
    response_model=list[TaskSummary],
    dependencies=[Depends(require("dev.jobs.manage", Scope.ALL))],
)
async def job_summary(
    session: DbDep, hours: Annotated[int, Query(ge=1, le=168)] = 24
) -> list[TaskSummary]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(
                JobRun.task_name,
                func.count(),
                func.count().filter(JobRun.status == JobStatus.FAILED),
                func.avg(JobRun.duration_ms),
                func.max(JobRun.created_at),
            )
            .where(JobRun.created_at >= since)
            .group_by(JobRun.task_name)
            .order_by(desc(func.count()))
        )
    ).all()

    return [
        TaskSummary(
            task_name=name,
            runs=int(runs),
            failures=int(failures),
            avg_duration_ms=int(avg) if avg is not None else None,
            last_run=last.isoformat() if last else None,
        )
        for name, runs, failures, avg, last in rows
    ]


# ── connectors ─────────────────────────────────────────────────────────


class ConnectorOut(BaseModel):
    name: str
    mode: str
    healthy: bool
    last_success_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    latency_ms: int | None = None
    detail: dict[str, Any] | None = None


@router.get("/connectors", response_model=list[ConnectorOut])
async def list_connectors(session: DbDep, settings: SettingsDep) -> list[ConnectorOut]:
    """Connector health. SharePoint always reports its write-guard state (C2)."""
    rows = (await session.scalars(select(ConnectorStatus))).all()

    known = {
        r.name: ConnectorOut(
            name=r.name,
            mode=r.mode.value,
            healthy=r.healthy,
            last_success_at=r.last_success_at.isoformat() if r.last_success_at else None,
            last_error=r.last_error,
            last_error_at=r.last_error_at.isoformat() if r.last_error_at else None,
            latency_ms=r.latency_ms,
            detail=r.detail,
        )
        for r in rows
    }

    # Report SharePoint even before its first sync, so the read-only badge is never absent.
    if "sharepoint" not in known:
        known["sharepoint"] = ConnectorOut(
            name="sharepoint",
            mode="read_only",
            healthy=True,
            detail={
                "note": "Live SharePoint is read-only (constraint C2).",
                "sandbox_site": settings.sharepoint_sandbox_site_path,
                "sandbox_writes_enabled": settings.sharepoint_sandbox_writes_enabled,
                "read_sites": list(settings.sharepoint_read_sites),
            },
        )

    return sorted(known.values(), key=lambda c: c.name)


class SyncResult(BaseModel):
    """What one ingest run did. Mirrors the summary the Celery task returns."""

    connector: str
    created: int = 0
    updated: int = 0
    archived: int = 0
    skipped: bool = False
    reason: str | None = None
    site_path: str | None = None


@router.post(
    "/connectors/sharepoint/sync",
    response_model=SyncResult,
    dependencies=[Depends(require("dev.jobs.manage", Scope.ALL))],
)
async def trigger_sharepoint_sync(session: DbDep, settings: SettingsDep) -> SyncResult:
    """Run the SharePoint ingest now, in-process.

    The scheduled sync needs Redis and a Celery worker, which RUNNING.md makes optional. This
    is how you pull live data without them — and how you see the real Graph error rather than
    a connector row that just says unhealthy. Read-only: same connector, same guard.
    """
    from app.services.sharepoint_sync import sync_proposals

    summary = await sync_proposals(session, settings=settings)
    return SyncResult(connector="sharepoint", **summary)


# ── rule inspector (§6.1) ──────────────────────────────────────────────


class RuleEvaluationOut(BaseModel):
    id: str
    decision_point: str
    rule_set_id: str | None = None
    rule_set_version: int | None = None
    team_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    facts: dict[str, Any]
    matched_rule_ids: list[str]
    trace: list[dict[str, Any]] | None = None
    outcome: dict[str, Any]
    simulated: bool
    duration_ms: int | None = None
    correlation_id: str | None = None
    created_at: str


@router.get("/rule-evaluations", response_model=Page[RuleEvaluationOut])
async def list_rule_evaluations(
    session: DbDep,
    page: PaginationDep,
    decision_point: Annotated[str | None, Query()] = None,
    entity_id: Annotated[str | None, Query()] = None,
    team_id: Annotated[uuid.UUID | None, Query()] = None,
    include_simulated: Annotated[bool, Query()] = False,
    correlation_id: Annotated[str | None, Query()] = None,
) -> Page[RuleEvaluationOut]:
    """Why the system decided what it decided.

    Filter by ``entity_id`` to answer "why did Rahul get *this* proposal?" — the trace shows
    the facts the engine saw and which rules matched.
    """
    query = select(RuleEvaluation)
    if decision_point:
        query = query.where(RuleEvaluation.decision_point == decision_point)
    if entity_id:
        query = query.where(RuleEvaluation.entity_id == entity_id)
    if team_id:
        query = query.where(RuleEvaluation.team_id == team_id)
    if correlation_id:
        query = query.where(RuleEvaluation.correlation_id == correlation_id)
    if not include_simulated:
        query = query.where(RuleEvaluation.simulated.is_(False))

    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (
        await session.scalars(
            query.order_by(desc(RuleEvaluation.created_at))
            .limit(page.limit)
            .offset(page.offset)
        )
    ).all()

    return Page.of(
        [
            RuleEvaluationOut(
                id=str(r.id),
                decision_point=r.decision_point,
                rule_set_id=str(r.rule_set_id) if r.rule_set_id else None,
                rule_set_version=r.rule_set_version,
                team_id=str(r.team_id) if r.team_id else None,
                entity_type=r.entity_type,
                entity_id=r.entity_id,
                facts=r.facts,
                matched_rule_ids=[str(x) for x in (r.matched_rule_ids or [])],
                trace=r.trace,
                outcome=r.outcome,
                simulated=r.simulated,
                duration_ms=r.duration_ms,
                correlation_id=r.correlation_id,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


# ── audit explorer ─────────────────────────────────────────────────────


class AuditOut(BaseModel):
    id: str
    actor_id: str | None = None
    team_id: str | None = None
    action: str
    entity_type: str
    entity_id: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    correlation_id: str | None = None
    created_at: str


@router.get("/audit", response_model=Page[AuditOut])
async def list_audit(
    session: DbDep,
    page: PaginationDep,
    actor_id: Annotated[uuid.UUID | None, Query()] = None,
    team_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    entity_id: Annotated[str | None, Query()] = None,
    correlation_id: Annotated[str | None, Query()] = None,
) -> Page[AuditOut]:
    query = audit_service.build_query(
        actor_id=actor_id,
        team_id=team_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        correlation_id=correlation_id,
    )
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (await session.scalars(query.limit(page.limit).offset(page.offset))).all()

    return Page.of(
        [
            AuditOut(
                id=str(r.id),
                actor_id=str(r.actor_id) if r.actor_id else None,
                team_id=str(r.team_id) if r.team_id else None,
                action=r.action,
                entity_type=r.entity_type,
                entity_id=r.entity_id,
                before=r.before,
                after=r.after,
                correlation_id=r.correlation_id,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


# ── outbox (§8.2) ──────────────────────────────────────────────────────


class OutboxOut(BaseModel):
    id: str
    to_addresses: list[str]
    subject: str
    body: str
    status: str
    created_at: str


@router.get("/outbox", response_model=Page[OutboxOut])
async def list_outbox(session: DbDep, page: PaginationDep) -> Page[OutboxOut]:
    """Mail captured instead of sent, outside production.

    This is what makes the email flow fully testable without anything reaching a real
    supplier.
    """
    query = select(EmailOutbox).order_by(desc(EmailOutbox.created_at))
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (await session.scalars(query.limit(page.limit).offset(page.offset))).all()

    return Page.of(
        [
            OutboxOut(
                id=str(r.id),
                to_addresses=[str(a) for a in r.to_addresses],
                subject=r.subject,
                body=r.body,
                status=r.status.value,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


# ── feature flags ──────────────────────────────────────────────────────


class FlagOut(BaseModel):
    key: str
    description: str | None = None
    enabled: bool
    rules: dict[str, Any] = Field(default_factory=dict)


class FlagIn(BaseModel):
    key: str = Field(min_length=2, max_length=80)
    description: str | None = None
    enabled: bool = False
    rules: dict[str, Any] = Field(default_factory=dict)


@router.get("/flags", response_model=list[FlagOut])
async def list_flags(session: DbDep) -> list[FlagOut]:
    rows = (await session.scalars(select(FeatureFlag).order_by(FeatureFlag.key))).all()
    return [
        FlagOut(key=r.key, description=r.description, enabled=r.enabled, rules=r.rules)
        for r in rows
    ]


@router.put(
    "/flags",
    response_model=Message,
    dependencies=[Depends(require("dev.flags.manage", Scope.ALL))],
)
async def upsert_flag(
    body: FlagIn, session: DbDep, principal: CurrentPrincipal
) -> Message:
    flag = await session.get(FeatureFlag, body.key)
    if flag is None:
        flag = FeatureFlag(key=body.key)
        session.add(flag)

    flag.description = body.description
    flag.enabled = body.enabled
    flag.rules = body.rules
    flag.updated_by = principal.user_id
    await session.flush()

    return Message(message=f"Flag {body.key!r} saved.")
