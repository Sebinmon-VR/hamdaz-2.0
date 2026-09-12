"""Workflows and their runs: what the routes, the worker and the seed call.

The engine walks a run; this is everything around it — who may start what,
where a run is kept, how a person's answer reaches a waiting step, and the
list screens. Every function takes a session and does no I/O beyond it, apart
from ``start`` and ``answer``, which hand the run to the engine and so may
reach the services the steps use.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.team import Team
from app.models.user import User
from app.models.workflow import (
    OPEN_RUN_STATUSES,
    FileSource,
    RunEventKind,
    RunStatus,
    Workflow,
    WorkflowRun,
    WorkflowRunFile,
    WorkflowSettings,
    WorkflowTrigger,
)
from app.teams import service as teams_service
from app.teams.service import TeamError
from app.workflows import catalogue
from app.workflows.engine import Services, WorkflowError, add_event, advance
from app.workflows.templating import condition_holds


class WorkflowNotFoundError(WorkflowError):
    pass


class WorkflowConflictError(WorkflowError):
    pass


# ── settings ───────────────────────────────────────────────────────────


async def get_settings(session: AsyncSession) -> WorkflowSettings:
    row = await session.get(WorkflowSettings, 1)
    if row is None:
        row = WorkflowSettings(id=1)
        session.add(row)
        await session.flush()
    return row


async def update_settings(
    session: AsyncSession, *, actor_id: uuid.UUID | None, changes: dict[str, Any]
) -> WorkflowSettings:
    row = await get_settings(session)
    for key in ("send_email", "write_sharepoint", "write_zoho", "poll_seconds"):
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    if "from_mailbox" in changes:
        value = (changes["from_mailbox"] or "").strip()
        row.from_mailbox = value or None
    row.updated_by_id = actor_id
    await session.flush()
    return row


# ── flows ──────────────────────────────────────────────────────────────


async def seed_flows(session: AsyncSession) -> int:
    """Create the shipped flows that are missing. An edited one is left alone."""
    existing = {w.key for w in (await session.scalars(select(Workflow))).all()}
    made = 0
    for spec in catalogue.FLOWS:
        if spec.key in existing:
            continue
        team: Team | None = None
        if spec.team_slug:
            try:
                team = await teams_service.get_team(session, spec.team_slug)
            except TeamError:
                team = None  # the team can be set later from the screen
        session.add(
            Workflow(
                key=spec.key, name=spec.name, description=spec.description,
                team_id=team.id if team else None, trigger=spec.trigger, enabled=True,
                steps=catalogue.validate_steps(spec.steps), is_system=True,
            )
        )
        made += 1
    await session.flush()
    return made


async def list_flows(session: AsyncSession, *, include_archived: bool = False) -> list[Workflow]:
    query = select(Workflow).order_by(Workflow.name)
    if not include_archived:
        query = query.where(Workflow.archived_at.is_(None))
    return list((await session.scalars(query)).all())


async def get_flow(session: AsyncSession, key: str | uuid.UUID) -> Workflow:
    if isinstance(key, uuid.UUID):
        row = await session.get(Workflow, key)
    else:
        try:
            row = await session.get(Workflow, uuid.UUID(str(key)))
        except ValueError:
            row = await session.scalar(select(Workflow).where(Workflow.key == str(key)))
    if row is None:
        raise WorkflowNotFoundError("No such workflow")
    return row


async def _team_for(session: AsyncSession, ref: str | None) -> Team | None:
    if not ref:
        return None
    try:
        return await teams_service.get_team(session, ref)
    except TeamError as exc:
        raise WorkflowError(f"No team called {ref!r}") from exc


async def create_flow(session: AsyncSession, *, payload: dict[str, Any], actor: User) -> Workflow:
    key = str(payload["key"]).strip().lower()
    if await session.scalar(select(Workflow).where(Workflow.key == key)):
        raise WorkflowConflictError(f"A workflow called {key!r} already exists")
    team = await _team_for(session, payload.get("team"))
    row = Workflow(
        key=key, name=payload["name"].strip(), description=payload.get("description"),
        team_id=team.id if team else None, trigger=payload.get("trigger") or WorkflowTrigger.MANUAL,
        enabled=bool(payload.get("enabled", True)),
        steps=catalogue.validate_steps(payload["steps"]),
        created_by_id=actor.id, updated_by_id=actor.id,
    )
    session.add(row)
    await session.flush()
    return row


async def update_flow(
    session: AsyncSession, flow: Workflow, *, changes: dict[str, Any], actor: User
) -> Workflow:
    if "name" in changes and changes["name"]:
        flow.name = changes["name"].strip()
    if "description" in changes:
        flow.description = changes["description"]
    if "team" in changes:
        team = await _team_for(session, changes["team"])
        flow.team_id = team.id if team else None
    if changes.get("trigger"):
        flow.trigger = changes["trigger"]
    if changes.get("enabled") is not None:
        flow.enabled = bool(changes["enabled"])
    if changes.get("steps") is not None:
        flow.steps = catalogue.validate_steps(changes["steps"])
        flow.version += 1
    flow.updated_by_id = actor.id
    await session.flush()
    return flow


async def archive_flow(session: AsyncSession, flow: Workflow) -> Workflow:
    flow.archived_at = datetime.now(UTC)
    flow.enabled = False
    await session.flush()
    return flow


async def restore_flow(session: AsyncSession, flow: Workflow) -> Workflow:
    flow.archived_at = None
    await session.flush()
    return flow


async def delete_flow(session: AsyncSession, flow: Workflow) -> None:
    if flow.is_system:
        raise WorkflowConflictError("The shipped workflow cannot be deleted; archive it instead.")
    if await session.scalar(
        select(func.count()).select_from(WorkflowRun).where(
            WorkflowRun.workflow_id == flow.id, WorkflowRun.status.in_(list(OPEN_RUN_STATUSES))
        )
    ):
        raise WorkflowConflictError("Runs are still going on this workflow.")
    await session.delete(flow)
    await session.flush()


async def open_run_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(WorkflowRun.workflow_id, func.count())
        .where(WorkflowRun.status.in_(list(OPEN_RUN_STATUSES)))
        .group_by(WorkflowRun.workflow_id)
    )
    return {wid: int(n) for wid, n in rows.all()}


async def flows_for(session: AsyncSession, *, user_id: uuid.UUID, is_admin: bool) -> list[Workflow]:
    """The enabled flows this person may start: any-team ones plus their teams'."""
    flows = [f for f in await list_flows(session) if f.enabled]
    if is_admin:
        return flows
    mine = {team.id for team, _ in await teams_service.teams_for_user(session, user_id)}
    return [f for f in flows if f.team_id is None or f.team_id in mine]


# ── runs ───────────────────────────────────────────────────────────────


def _tag() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "HZ-" + "".join(secrets.choice(alphabet) for _ in range(6))


async def _unique_tag(session: AsyncSession) -> str:
    for _ in range(10):
        tag = _tag()
        if not await session.scalar(select(WorkflowRun.id).where(WorkflowRun.tag == tag)):
            return tag
    raise WorkflowError("Could not make a unique run tag")


async def open_run_for(
    session: AsyncSession, *, workflow_id: uuid.UUID, subject_id: str
) -> WorkflowRun | None:
    return await session.scalar(
        select(WorkflowRun).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.subject_id == subject_id,
            WorkflowRun.status.in_(list(OPEN_RUN_STATUSES)),
        )
    )


async def start(
    session: AsyncSession,
    flow: Workflow,
    *,
    owner: User,
    subject_id: str,
    subject_label: str | None,
    settings: WorkflowSettings,
    services: Services,
) -> WorkflowRun:
    """Begin a run and walk it as far as it goes before something has to wait.

    One open run per flow per subject: starting the presales flow twice on
    the same task would mail every supplier twice.
    """
    if not flow.enabled or flow.archived_at is not None:
        raise WorkflowError("That workflow is switched off")
    if await open_run_for(session, workflow_id=flow.id, subject_id=subject_id):
        raise WorkflowConflictError("A run is already going for this task on this workflow")
    run = WorkflowRun(
        workflow=flow,
        workflow_version=flow.version,
        steps=list(flow.steps or []),
        subject_kind=flow.subject_kind,
        subject_id=str(subject_id),
        subject_label=(subject_label or None),
        owner=owner,
        team_id=flow.team_id,
        tag=await _unique_tag(session),
        status=RunStatus.RUNNING,
        context={},
        events=[],
        files=[],
        messages=[],
    )
    session.add(run)
    await session.flush()
    run.context = {
        "run": {"id": str(run.id), "tag": run.tag, "team": None, "workflow": flow.key},
        "owner": {"id": str(owner.id), "name": owner.display_name, "email": owner.email},
        "task": {"id": str(subject_id), "title": subject_label or str(subject_id)},
        "_answers": {},
    }
    if flow.team_id:
        team = await session.get(Team, flow.team_id)
        if team is not None:
            run.context["run"]["team"] = team.slug
    await add_event(session, run, RunEventKind.STARTED, payload={"workflow": flow.key, "version": flow.version}, by=owner.id)
    await advance(session, run, settings, services, by=owner.id)
    return run


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> WorkflowRun:
    row = await session.scalar(
        select(WorkflowRun)
        .options(
            selectinload(WorkflowRun.events),
            selectinload(WorkflowRun.files),
            selectinload(WorkflowRun.messages),
        )
        .where(WorkflowRun.id == run_id)
    )
    if row is None:
        raise WorkflowNotFoundError("No such run")
    return row


async def list_runs(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID | None = None,
    team_ids: list[uuid.UUID] | None = None,
    subject_id: str | None = None,
    workflow_id: uuid.UUID | None = None,
    open_only: bool = False,
    limit: int = 100,
) -> list[WorkflowRun]:
    query = select(WorkflowRun).order_by(WorkflowRun.started_at.desc()).limit(limit)
    if owner_id is not None and team_ids is not None:
        query = query.where(
            (WorkflowRun.owner_id == owner_id) | (WorkflowRun.team_id.in_(team_ids))
        )
    elif owner_id is not None:
        query = query.where(WorkflowRun.owner_id == owner_id)
    elif team_ids is not None:
        query = query.where(WorkflowRun.team_id.in_(team_ids))
    if subject_id:
        query = query.where(WorkflowRun.subject_id == str(subject_id))
    if workflow_id:
        query = query.where(WorkflowRun.workflow_id == workflow_id)
    if open_only:
        query = query.where(WorkflowRun.status.in_(list(OPEN_RUN_STATUSES)))
    return list((await session.scalars(query)).all())


async def add_upload(
    session: AsyncSession, run: WorkflowRun, *, file_name: str, content: bytes,
    content_type: str | None,
) -> WorkflowRunFile:
    """A file the person attached while answering. Kept against the waiting step."""
    if run.status != RunStatus.WAITING_USER or not run.pending:
        raise WorkflowConflictError("The run is not waiting for anything from you")
    if not run.pending.get("allow_files"):
        raise WorkflowConflictError("This step does not take files")
    row = WorkflowRunFile(
        run_id=run.id, step_key=run.pending.get("step_key"), source=FileSource.UPLOAD,
        file_name=file_name[:255], content_type=content_type, size=len(content), content=content,
        origin=run.owner.display_name if run.owner else None,
    )
    session.add(row)
    await session.flush()
    return row


async def answer(
    session: AsyncSession,
    run: WorkflowRun,
    *,
    user: User,
    values: dict[str, Any],
    value: Any,
    settings: WorkflowSettings,
    services: Services,
) -> WorkflowRun:
    """The person's answer to the step the run is waiting on, then carry on."""
    if run.status != RunStatus.WAITING_USER or not run.pending:
        raise WorkflowConflictError("The run is not waiting for anything from you")
    step_key = str(run.pending.get("step_key") or "")
    uploaded = [
        f.file_name for f in await session.scalars(
            select(WorkflowRunFile).where(
                WorkflowRunFile.run_id == run.id, WorkflowRunFile.step_key == step_key,
                WorkflowRunFile.source == FileSource.UPLOAD,
            )
        )
    ]
    answers = dict(run.context.get("_answers") or {})
    answers[step_key] = {
        "values": values or {},
        "value": value,
        "files": uploaded,
        "by": str(user.id),
        "at": datetime.now(UTC).isoformat(),
    }
    run.context = {**run.context, "_answers": answers}
    await add_event(
        session, run, RunEventKind.ANSWERED, step_key=step_key,
        payload={"mode": run.pending.get("mode"), "files": uploaded,
                 "fields": sorted((values or {}).keys())},
        by=user.id,
    )
    return await advance(session, run, settings, services, by=user.id)


async def cancel(session: AsyncSession, run: WorkflowRun, *, user: User) -> WorkflowRun:
    if not run.is_open:
        raise WorkflowConflictError(f"The run already finished ({run.status})")
    run.status = RunStatus.CANCELLED
    run.pending = None
    run.wake_at = None
    run.finished_at = datetime.now(UTC)
    run.cancelled_by_id = user.id
    await add_event(session, run, RunEventKind.CANCELLED, by=user.id)
    await session.flush()
    return run


async def retry(
    session: AsyncSession, run: WorkflowRun, *, user: User,
    settings: WorkflowSettings, services: Services,
) -> WorkflowRun:
    """Try the failed step again. Whatever it had already done stays done."""
    if run.status != RunStatus.FAILED:
        raise WorkflowConflictError("Only a failed run can be retried")
    run.status = RunStatus.RUNNING
    run.error = None
    run.finished_at = None
    await add_event(session, run, RunEventKind.RETRIED, step_key=(run.current_step or {}).get("key"), by=user.id)
    return await advance(session, run, settings, services, by=user.id)


async def auto_start(
    session: AsyncSession, *, settings: WorkflowSettings, services: Services, limit: int = 5
) -> list[WorkflowRun]:
    """Start runs for tasks newly assigned to a team whose flow is on ``task_assigned``.

    Reads the local mirror of the Proposals list rather than SharePoint, so a
    tick costs nothing outside this database. A task gets one run per flow,
    ever: a run that finished, failed or was cancelled is not started again by
    a timer — that is a person's call, from the screen.
    """
    from sqlalchemy import exists

    from app.models.proposal_index import ProposalIndexItem

    flows = [
        f for f in await list_flows(session)
        if f.enabled and f.trigger == WorkflowTrigger.TASK_ASSIGNED and f.team_id is not None
    ]
    started: list[WorkflowRun] = []
    for flow in flows:
        members = {
            (user.email or "").lower(): user
            for user, _ in await teams_service.list_members(session, flow.team_id)
            if user.email
        }
        if not members:
            continue
        seen = exists().where(
            WorkflowRun.workflow_id == flow.id,
            WorkflowRun.subject_id == ProposalIndexItem.item_id,
        )
        tasks = await session.scalars(
            select(ProposalIndexItem)
            .where(
                ProposalIndexItem.is_open.is_(True),
                ProposalIndexItem.deleted.is_(False),
                func.lower(ProposalIndexItem.assigned_email).in_(list(members)),
                ~seen,
            )
            .order_by(ProposalIndexItem.sp_created_at.desc())
            .limit(limit)
        )
        for task in tasks.all():
            owner = members.get((task.assigned_email or "").lower())
            if owner is None:
                continue
            try:
                run = await start(
                    session, flow, owner=owner, subject_id=task.item_id,
                    subject_label=task.title, settings=settings, services=services,
                )
            except WorkflowError:
                continue
            started.append(run)
    return started


async def wake_due(session: AsyncSession, *, limit: int = 20) -> list[WorkflowRun]:
    """Runs waiting on the world whose next look is due."""
    now = datetime.now(UTC)
    rows = await session.scalars(
        select(WorkflowRun)
        .where(
            WorkflowRun.status == RunStatus.WAITING_EVENT,
            (WorkflowRun.wake_at.is_(None)) | (WorkflowRun.wake_at <= now),
        )
        .order_by(WorkflowRun.wake_at)
        .limit(limit)
    )
    return list(rows.all())


# ── presenting ─────────────────────────────────────────────────────────


def step_states(run: WorkflowRun) -> list[dict[str, Any]]:
    """Each step with where the run is on it, for the timeline."""
    notes: dict[str, str | None] = {}
    skipped: set[str] = set()
    for event in run.events or []:
        if event.kind == RunEventKind.STEP_COMPLETED and event.step_key:
            notes[event.step_key] = (event.payload or {}).get("note")
        if event.kind == RunEventKind.STEP_SKIPPED and event.step_key:
            skipped.add(event.step_key)
        if event.kind == RunEventKind.WAITING and event.step_key:
            notes[event.step_key] = (event.payload or {}).get("note")
    out = []
    for index, step in enumerate(run.steps or []):
        key = str(step.get("key"))
        if key in skipped:
            state = "skipped"
        elif index < run.step_index:
            state = "done"
        elif index == run.step_index:
            if run.status == RunStatus.FAILED:
                state = "failed"
            elif run.status in (RunStatus.WAITING_USER, RunStatus.WAITING_EVENT):
                state = "waiting"
            elif run.status == RunStatus.RUNNING:
                state = "running"
            elif run.status == RunStatus.COMPLETED:
                state = "done"
            else:
                state = "pending"
        else:
            # Foretold: a later step whose condition already reads false is
            # shown as one that will be skipped, so the timeline is honest
            # about how many steps are really left.
            state = "pending"
            when = step.get("when")
            if when and not condition_holds(when, run.context) and run.status != RunStatus.COMPLETED:
                state = "skipped"
        out.append({"key": key, "kind": step.get("kind"), "name": step.get("name"), "state": state, "note": notes.get(key)})
    return out
