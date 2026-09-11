"""Projects as database operations: planning one, working it, and recording it.

The rules live here rather than in the router because they have to hold however
the change arrives — the page, the API, or the assistant acting on somebody's
behalf. Every one of them is enforced against ``app.projects.access``, which is
the only place that decides anything about permission.

Three properties this file is responsible for:

* **every movement is logged.** Changing a task's percentage or status writes a
  ``ProjectUpdate`` beside it. That log is what a report over a past window
  reads, and a change that skipped it would be a change no report can ever
  mention;
* **assignment implies membership.** Giving somebody a task puts them on the
  project, so there is exactly one answer to who can see it;
* **nothing derives a number twice.** Percentages, overdue counts and health
  suggestions all come from ``app.projects.progress``. A listing loads the rows
  and calls those functions rather than reimplementing them in SQL — see
  ``summaries`` for why that trade is the right way round.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.notification import NotificationKind
from app.models.project import (
    CLOSED_PROJECT_STATUSES,
    OPEN_ISSUE_STATUSES,
    OPEN_TASK_STATUSES,
    IssueStatus,
    MilestonePlan,
    Project,
    ProjectIssue,
    ProjectMember,
    ProjectMilestone,
    ProjectRole,
    ProjectStatus,
    ProjectTask,
    ProjectUpdate,
    Rag,
    RagTrend,
    TaskPriority,
    TaskStatus,
    UpdateKind,
)
from app.models.user import User
from app.notifications import service as notifications
from app.projects import progress
from app.projects.access import TEAM_OVERSIGHT, Viewer
from app.roles.service import global_role_keys
from app.teams import service as teams_service


class ProjectError(Exception):
    """An operation was refused. The message is safe to show a person."""


class ProjectNotFoundError(ProjectError):
    pass


class ProjectPermissionError(ProjectError):
    pass


class ProjectConflictError(ProjectError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _today() -> date:
    return _now().date()


# ── the caller ─────────────────────────────────────────────────────────


async def build_viewer(session: AsyncSession, user: User) -> Viewer:
    """Everything the access rules need about the caller, gathered once.

    Two queries beyond the role and team lookups the rest of the app already
    does: which projects they are on, and which of those they lead. Both are
    small — nobody is a member of a thousand projects — and having them up
    front is what lets the listing filter in SQL instead of loading everything
    and discarding most of it.
    """
    roles = set(await global_role_keys(session, user.id))
    memberships = await teams_service.teams_for_user(session, user.id)

    rows = (
        await session.execute(
            select(ProjectMember.project_id, ProjectMember.role).where(
                ProjectMember.user_id == user.id
            )
        )
    ).all()

    return Viewer(
        user_id=user.id,
        roles=frozenset(roles),
        team_ids=frozenset(team.id for team, _ in memberships),
        oversees=frozenset(
            team.id for team, keys in memberships if not TEAM_OVERSIGHT.isdisjoint(keys)
        ),
        member_of=frozenset(pid for pid, _ in rows),
        leads=frozenset(pid for pid, role in rows if role == ProjectRole.LEAD),
    )


# ── the visible set ────────────────────────────────────────────────────


def visible(query: Select, viewer: Viewer) -> Select:
    """Narrow a query over projects to the ones this person may read.

    Company-wide roles get no clause at all, deliberately: an enumerated list
    of team ids would go stale the moment a team was created, and the CEO
    silently not seeing a new team's work is the kind of bug nobody reports
    because nobody knows to look for it.
    """
    if viewer.is_company_wide:
        return query

    clauses = [Project.lead_id == viewer.user_id]
    if viewer.oversees:
        clauses.append(Project.team_id.in_(viewer.oversees))
    if viewer.member_of:
        clauses.append(Project.id.in_(viewer.member_of))
    return query.where(or_(*clauses))


def _loaded(query: Select) -> Select:
    """Pull in the rows a project is never useful without.

    ``members`` and ``milestones`` come along by default (see the model);
    tasks, issues and the update log do not, so anything wanting the whole
    project asks for them here. Loading them in one place rather than at each
    call site is what stops a new endpoint tripping over a lazy load inside an
    async request — which surfaces as ``MissingGreenlet`` and reads like a
    driver bug rather than a missing option.
    """
    return query.options(
        selectinload(Project.tasks).selectinload(ProjectTask.assignee),
        selectinload(Project.issues).selectinload(ProjectIssue.owner),
    )


async def get(session: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await session.scalar(_loaded(select(Project)).where(Project.id == project_id))
    if project is None:
        raise ProjectNotFoundError("That project does not exist.")
    return project


async def get_for(session: AsyncSession, project_id: uuid.UUID, viewer: Viewer) -> Project:
    """Fetch a project this person may read, or refuse as if it did not exist.

    **404, not 403**, exactly as the reports module does it: a 403 on a project
    id confirms that the id names a real project of some team, which is itself
    something the caller is not entitled to know.
    """
    from app.projects.access import may_read

    project = await get(session, project_id)
    if not may_read(project, viewer):
        raise ProjectNotFoundError("That project does not exist.")
    return project


# ── creating and changing one ──────────────────────────────────────────


async def create(
    session: AsyncSession,
    *,
    author: User,
    team_id: uuid.UUID,
    name: str,
    code: str | None = None,
    description: str | None = None,
    objective: str | None = None,
    status: str = ProjectStatus.PLANNED,
    lead_id: uuid.UUID | None = None,
    start_on: date | None = None,
    target_end_on: date | None = None,
    budget_amount: Decimal | None = None,
    currency: str = "AED",
) -> Project:
    """Start a project. The lead is put on it as lead in the same breath.

    Creating it does not assess it: every dial starts grey and
    ``health_reviewed_at`` stays null, so a brand new project is visibly
    unassessed rather than reassuringly green. That is the single most
    important default in the module — see ``Rag.GREY``.
    """
    name = (name or "").strip()
    if not name:
        raise ProjectError("A project needs a name.")
    if status not in set(ProjectStatus):
        raise ProjectError(f"status must be one of: {', '.join(ProjectStatus)}")
    if start_on and target_end_on and target_end_on < start_on:
        raise ProjectError("The target end date is before the start date.")

    code = (code or "").strip() or None
    if code is not None:
        clash = await session.scalar(
            select(Project).where(Project.team_id == team_id, Project.code == code)
        )
        if clash is not None:
            raise ProjectConflictError(f"This team already has a project coded {code}.")

    project = Project(
        team_id=team_id,
        code=code,
        name=name,
        description=description,
        objective=objective,
        status=status,
        lead_id=lead_id,
        start_on=start_on,
        target_end_on=target_end_on,
        budget_amount=budget_amount,
        currency=(currency or "AED").upper()[:3],
        created_by_id=author.id,
    )
    # Filled in while the project is still transient, and the order matters:
    # once flushed it is a persistent object whose collections are not loaded,
    # so appending would emit a SELECT — and that from synchronous code inside
    # an async session is a MissingGreenlet rather than a query.
    if lead_id is not None:
        project.members.append(
            ProjectMember(user_id=lead_id, role=ProjectRole.LEAD, added_by_id=author.id)
        )
    session.add(project)
    await session.flush()
    return project


async def update(
    session: AsyncSession,
    project: Project,
    *,
    actor: User,
    **changes: Any,
) -> Project:
    """Change a project. Only the keys present in ``changes`` are touched.

    ``None`` is a real value here — clearing a target date is something people
    do — so "present" means present in the mapping, not truthy. Callers build
    that mapping from a schema's ``exclude_unset``, which is what makes the
    distinction survive the trip over HTTP.
    """
    simple = {
        "name", "code", "description", "objective", "status", "lead_id",
        "start_on", "target_end_on", "actual_end_on", "budget_amount",
        "spend_amount", "currency", "percent_complete",
    }
    unknown = set(changes) - simple
    if unknown:
        raise ProjectError(f"Cannot change: {', '.join(sorted(unknown))}")

    if "status" in changes and changes["status"] not in set(ProjectStatus):
        raise ProjectError(f"status must be one of: {', '.join(ProjectStatus)}")

    if "code" in changes:
        code = (changes["code"] or "").strip() or None
        if code is not None and code != project.code:
            clash = await session.scalar(
                select(Project).where(
                    Project.team_id == project.team_id,
                    Project.code == code,
                    Project.id != project.id,
                )
            )
            if clash is not None:
                raise ProjectConflictError(f"This team already has a project coded {code}.")
        changes["code"] = code

    if "name" in changes:
        name = (changes["name"] or "").strip()
        if not name:
            raise ProjectError("A project needs a name.")
        changes["name"] = name

    was_status = project.status
    for key, value in changes.items():
        setattr(project, key, value)

    start, end = project.start_on, project.target_end_on
    if start and end and end < start:
        raise ProjectError("The target end date is before the start date.")

    # A project that has just been finished dates itself, so nobody has to
    # remember to. Only when it was not already closed, so re-saving a done
    # project does not keep moving the date it landed on.
    if (
        project.status in CLOSED_PROJECT_STATUSES
        and was_status not in CLOSED_PROJECT_STATUSES
        and project.actual_end_on is None
    ):
        project.actual_end_on = _today()

    if "lead_id" in changes and project.lead_id is not None:
        await set_member(
            session, project, user_id=project.lead_id, role=ProjectRole.LEAD, actor=actor
        )

    if was_status != project.status:
        _log(
            session, project,
            kind=UpdateKind.HEALTH, actor=actor, subject=project.name,
            status_before=was_status, status_after=project.status,
            body=f"Project moved to {project.status.replace('_', ' ')}.",
        )

    await session.flush()
    return project


async def set_health(
    session: AsyncSession,
    project: Project,
    *,
    actor: User,
    note: str | None = None,
    **dials: Any,
) -> Project:
    """Record the lead's assessment of the five dials, and stamp when.

    The timestamp is the point of this being its own operation rather than part
    of ``update``. Dials that have not been looked at for a fortnight are shown
    as stale (see ``progress.health_is_stale``), and that is only possible if
    reviewing them is a distinct act — saving a project's description should
    not silently claim its health was reassessed.

    Confirming the dials unchanged still counts as a review, and deliberately
    so: "looked at it, still amber" is information.
    """
    allowed_rag = {"rag_overall", "rag_scope", "rag_cost", "rag_schedule", "rag_benefits"}
    allowed_trend = {
        "trend_overall", "trend_scope", "trend_cost", "trend_schedule", "trend_benefits"
    }
    unknown = set(dials) - allowed_rag - allowed_trend
    if unknown:
        raise ProjectError(f"Not a health dial: {', '.join(sorted(unknown))}")

    for key, value in dials.items():
        if value is None:
            continue
        if key in allowed_rag and value not in set(Rag):
            raise ProjectError(f"{key} must be one of: {', '.join(Rag)}")
        if key in allowed_trend and value not in set(RagTrend):
            raise ProjectError(f"{key} must be one of: {', '.join(RagTrend)}")
        setattr(project, key, value)

    before = project.rag_overall
    project.health_reviewed_at = _now()
    if note is not None:
        project.health_note = note

    _log(
        session, project,
        kind=UpdateKind.HEALTH, actor=actor, subject=project.name,
        status_before=before, status_after=project.rag_overall,
        body=note or f"Health reviewed — overall {project.rag_overall}.",
    )
    await session.flush()
    return project


async def archive(session: AsyncSession, project: Project) -> Project:
    project.archived_at = _now()
    await session.flush()
    return project


async def restore(session: AsyncSession, project: Project) -> Project:
    project.archived_at = None
    await session.flush()
    return project


async def delete(session: AsyncSession, project: Project) -> None:
    """Remove a project and everything under it.

    Cascades to tasks, milestones, issues, members and the update log. Reports
    already filed against it are **not** deleted — their project line holds a
    copy of what it said at the time, so the record survives the project. See
    ``ReportProjectLine``.
    """
    await session.delete(project)
    await session.flush()


# ── people on it ───────────────────────────────────────────────────────


async def set_member(
    session: AsyncSession,
    project: Project,
    *,
    user_id: uuid.UUID,
    role: str = ProjectRole.MEMBER,
    responsibility: str | None = None,
    actor: User | None = None,
) -> ProjectMember:
    """Put somebody on the project, or change what they are on it.

    Idempotent: calling it for somebody already there updates their row rather
    than failing. That is what lets ``assign`` call it unconditionally without
    first checking, which in turn is what keeps "assignment implies membership"
    true in every path rather than in the ones somebody remembered.
    """
    if role not in set(ProjectRole):
        raise ProjectError(f"role must be one of: {', '.join(ProjectRole)}")

    for row in project.members:
        if row.user_id == user_id:
            row.role = role
            if responsibility is not None:
                row.responsibility = responsibility
            await session.flush()
            return row

    row = ProjectMember(
        project_id=project.id,
        user_id=user_id,
        role=role,
        responsibility=responsibility,
        added_by_id=actor.id if actor else None,
    )
    session.add(row)
    await session.flush()
    project.members.append(row)
    return row


async def remove_member(
    session: AsyncSession, project: Project, *, user_id: uuid.UUID
) -> None:
    """Take somebody off a project, refusing while they still hold work.

    Refused rather than cascaded on purpose. Silently unassigning their tasks
    would leave a plan full of ownerless work that looks assigned on nobody's
    dashboard; the lead is asked to decide where it goes, which is a decision
    only they can make.
    """
    held = await session.scalar(
        select(func.count())
        .select_from(ProjectTask)
        .where(
            ProjectTask.project_id == project.id,
            ProjectTask.assignee_id == user_id,
            ProjectTask.status.in_(OPEN_TASK_STATUSES),
        )
    )
    if held:
        raise ProjectConflictError(
            f"They still hold {held} open task{'s' if held > 1 else ''} here. "
            "Reassign those first."
        )

    # Removed from the loaded collection and left to ``delete-orphan`` to
    # issue the DELETE. A bulk DELETE *and* a collection removal would be two
    # attempts at the same row — the second on something the session already
    # believes is gone — which is the sort of thing that works until it does
    # not. One mechanism, and the identity map stays truthful.
    project.members[:] = [m for m in project.members if m.user_id != user_id]
    if project.lead_id == user_id:
        project.lead_id = None
    await session.flush()


# ── milestones ─────────────────────────────────────────────────────────


async def add_milestone(
    session: AsyncSession,
    project: Project,
    *,
    name: str,
    detail: str | None = None,
    owner_id: uuid.UUID | None = None,
    start_on: date | None = None,
    due_on: date | None = None,
    is_key: bool = False,
    actor: User | None = None,
) -> ProjectMilestone:
    name = (name or "").strip()
    if not name:
        raise ProjectError("A milestone needs a name.")
    if start_on and due_on and due_on < start_on:
        raise ProjectError("The milestone ends before it starts.")

    row = ProjectMilestone(
        project_id=project.id,
        position=len(project.milestones),
        name=name,
        detail=detail,
        owner_id=owner_id,
        start_on=start_on,
        due_on=due_on,
        # The plan's original date, captured now. Never written again — see
        # ``update_milestone`` — so slippage stays measurable from the first
        # date anybody committed to rather than from the most recent one.
        baseline_due_on=due_on,
        is_key=is_key,
    )
    session.add(row)
    await session.flush()
    project.milestones.append(row)

    if owner_id is not None:
        await set_member(session, project, user_id=owner_id, actor=actor)
    return row


async def update_milestone(
    session: AsyncSession,
    project: Project,
    milestone: ProjectMilestone,
    *,
    actor: User,
    **changes: Any,
) -> ProjectMilestone:
    """Change a milestone, keeping its baseline and logging any move.

    Moving a date is the single most reportable thing that happens to a plan,
    so it writes to the log with both dates in the body. A rescheduling nobody
    can see afterwards is how a project arrives four months late having been
    green throughout.
    """
    allowed = {
        "name", "detail", "owner_id", "start_on", "due_on", "done_on",
        "percent_complete", "plan", "is_key", "position",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ProjectError(f"Cannot change: {', '.join(sorted(unknown))}")

    if "plan" in changes and changes["plan"] not in set(MilestonePlan):
        raise ProjectError(f"plan must be one of: {', '.join(MilestonePlan)}")
    if "percent_complete" in changes:
        changes["percent_complete"] = _percent(changes["percent_complete"]) or 0

    was_due = milestone.due_on
    was_done = milestone.is_done

    for key, value in changes.items():
        setattr(milestone, key, value)

    # Set once, on the first date the milestone ever had. A milestone created
    # without a date and given one later baselines then; one that already had
    # a baseline keeps it however often it moves afterwards.
    if milestone.baseline_due_on is None and milestone.due_on is not None:
        milestone.baseline_due_on = was_due or milestone.due_on

    if milestone.start_on and milestone.due_on and milestone.due_on < milestone.start_on:
        raise ProjectError("The milestone ends before it starts.")

    if "due_on" in changes and was_due != milestone.due_on:
        slip = progress.slip_days(milestone)
        detail = f" ({slip:+d} days from plan)" if slip else ""
        _log(
            session, project,
            kind=UpdateKind.MILESTONE, actor=actor, subject=milestone.name,
            milestone_id=milestone.id,
            body=f"Moved from {was_due or 'no date'} to "
                 f"{milestone.due_on or 'no date'}{detail}.",
        )
    if not was_done and milestone.is_done:
        _log(
            session, project,
            kind=UpdateKind.MILESTONE, actor=actor, subject=milestone.name,
            milestone_id=milestone.id,
            percent_after=100,
            body=f"Milestone reached: {milestone.name}.",
        )

    if "owner_id" in changes and milestone.owner_id is not None:
        await set_member(session, project, user_id=milestone.owner_id, actor=actor)

    await session.flush()
    return milestone


async def delete_milestone(
    session: AsyncSession, project: Project, milestone: ProjectMilestone
) -> None:
    """Remove a milestone. The work under it stays, orphaned rather than lost.

    ``SET NULL`` on the task's ``milestone_id`` (see the model) rather than a
    cascade, because deleting a milestone is a re-plan and the tasks beneath it
    are usually the reason for it.
    """
    project.milestones[:] = [m for m in project.milestones if m.id != milestone.id]
    await session.flush()


# ── tasks ──────────────────────────────────────────────────────────────


def _percent(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProjectError("A percentage must be a whole number.") from exc
    return max(0, min(100, number))


async def add_task(
    session: AsyncSession,
    project: Project,
    *,
    title: str,
    actor: User,
    detail: str | None = None,
    milestone_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    status: str = TaskStatus.NOT_STARTED,
    priority: str = TaskPriority.MEDIUM,
    start_on: date | None = None,
    due_on: date | None = None,
    estimate_hours: Decimal | None = None,
) -> ProjectTask:
    """Add one task, and tell whoever it lands on.

    The notification is raised here rather than by the router so that every way
    a task can be created — the page, the assistant, a future import — tells
    the person. A task somebody is not told about is a task that gets done late
    and blamed on the tool.
    """
    title = (title or "").strip()
    if not title:
        raise ProjectError("A task needs a title.")
    if status not in set(TaskStatus):
        raise ProjectError(f"status must be one of: {', '.join(TaskStatus)}")
    if priority not in set(TaskPriority):
        raise ProjectError(f"priority must be one of: {', '.join(TaskPriority)}")
    if milestone_id is not None and not any(m.id == milestone_id for m in project.milestones):
        raise ProjectError("That milestone is not on this project.")

    task = ProjectTask(
        project_id=project.id,
        milestone_id=milestone_id,
        position=await _next_position(session, project.id),
        title=title,
        detail=detail,
        assignee_id=assignee_id,
        status=status,
        priority=priority,
        percent_complete=100 if status == TaskStatus.DONE else 0,
        start_on=start_on,
        due_on=due_on,
        done_at=_now() if status == TaskStatus.DONE else None,
        estimate_hours=estimate_hours,
        created_by_id=actor.id,
    )
    session.add(task)
    await session.flush()

    if assignee_id is not None:
        await set_member(session, project, user_id=assignee_id, actor=actor)
        await _tell_assignee(session, project, task, assignee_id)
    return task


async def _next_position(session: AsyncSession, project_id: uuid.UUID) -> int:
    highest = await session.scalar(
        select(func.max(ProjectTask.position)).where(ProjectTask.project_id == project_id)
    )
    return (highest or -1) + 1


async def update_task(
    session: AsyncSession,
    project: Project,
    task: ProjectTask,
    *,
    actor: User,
    note: str | None = None,
    hours: Decimal | None = None,
    **changes: Any,
) -> ProjectTask:
    """Move a task along, and write down that it moved.

    This is the operation an ordinary member is allowed, and the one a status
    report is ultimately built from. Whenever the percentage or the status
    changes it writes a ``ProjectUpdate`` carrying both the before and the
    after — which is what makes "what moved last week" answerable next month,
    after the task has moved four more times.

    A note with no change is still recorded. "Nothing moved, here is why" is
    the most useful line on a lot of weekly reports.
    """
    allowed = {
        "title", "detail", "milestone_id", "assignee_id", "status", "priority",
        "percent_complete", "start_on", "due_on", "estimate_hours",
        "spent_hours", "blocked_reason", "position",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ProjectError(f"Cannot change: {', '.join(sorted(unknown))}")

    if "status" in changes and changes["status"] not in set(TaskStatus):
        raise ProjectError(f"status must be one of: {', '.join(TaskStatus)}")
    if "priority" in changes and changes["priority"] not in set(TaskPriority):
        raise ProjectError(f"priority must be one of: {', '.join(TaskPriority)}")
    if changes.get("milestone_id") is not None and not any(
        m.id == changes["milestone_id"] for m in project.milestones
    ):
        raise ProjectError("That milestone is not on this project.")
    if "percent_complete" in changes:
        changes["percent_complete"] = _percent(changes["percent_complete"]) or 0

    was_status, was_percent = task.status, task.percent_complete
    was_assignee = task.assignee_id

    for key, value in changes.items():
        setattr(task, key, value)

    # The two directions of "done", kept in step so neither can lie. Marking a
    # task done fills the percentage in; dragging it to 100 marks it done. A
    # task at 100% that still says in progress is the sort of row that makes
    # people stop believing the board.
    if was_status != task.status:
        if task.status == TaskStatus.DONE:
            task.done_at = _now()
            if "percent_complete" not in changes:
                task.percent_complete = 100
        elif was_status == TaskStatus.DONE:
            task.done_at = None
            if "percent_complete" not in changes and task.percent_complete >= 100:
                task.percent_complete = 90
    elif task.percent_complete >= 100 and task.status in OPEN_TASK_STATUSES:
        task.status = TaskStatus.DONE
        task.done_at = _now()

    if hours is not None:
        task.spent_hours = (task.spent_hours or Decimal("0")) + hours

    moved = was_status != task.status or was_percent != task.percent_complete
    if moved or note:
        _log(
            session, project,
            kind=UpdateKind.TASK, actor=actor, subject=task.title,
            task_id=task.id,
            percent_before=was_percent if moved else None,
            percent_after=task.percent_complete if moved else None,
            status_before=was_status if was_status != task.status else None,
            status_after=task.status if was_status != task.status else None,
            hours=hours,
            body=note,
        )

    if task.assignee_id is not None and task.assignee_id != was_assignee:
        await set_member(session, project, user_id=task.assignee_id, actor=actor)
        await _tell_assignee(session, project, task, task.assignee_id)

    await session.flush()
    return task


async def assign(
    session: AsyncSession,
    project: Project,
    task: ProjectTask,
    *,
    assignee_id: uuid.UUID | None,
    actor: User,
) -> ProjectTask:
    """Hand a task to somebody — or to nobody, which is how it is taken back."""
    return await update_task(session, project, task, actor=actor, assignee_id=assignee_id)


async def delete_task(session: AsyncSession, project: Project, task: ProjectTask) -> None:
    """Remove a task. Its history stays as project history.

    ``SET NULL`` on the log's ``task_id`` rather than a cascade, so a report
    filed last month against work somebody has since deleted still has
    something behind it.
    """
    await session.delete(task)
    await session.flush()


async def _tell_assignee(
    session: AsyncSession, project: Project, task: ProjectTask, user_id: uuid.UUID
) -> None:
    """Raise the in-app notification for a task landing on somebody.

    Deduplicated on the task id, so reassigning back and forth updates one
    notification rather than stacking them. A failure to notify never fails the
    assignment — the task is the record, the bell is a convenience.
    """
    await notifications.notify(
        session,
        users=[user_id],
        kind=NotificationKind.TASK_ASSIGNED,
        title=f"{task.title}",
        body=f"Assigned to you on {project.label}."
        + (f" Due {task.due_on:%d %b %Y}." if task.due_on else ""),
        link=f"/projects/{project.id}/tasks/{task.id}",
        source="projects",
        source_id=str(task.id),
        payload={"project_id": str(project.id), "task_id": str(task.id)},
    )


# ── issues ─────────────────────────────────────────────────────────────


async def add_issue(
    session: AsyncSession,
    project: Project,
    *,
    title: str,
    actor: User,
    detail: str | None = None,
    priority: str = TaskPriority.MEDIUM,
    owner_id: uuid.UUID | None = None,
    due_on: date | None = None,
    needs_support: bool = False,
    support_note: str | None = None,
) -> ProjectIssue:
    title = (title or "").strip()
    if not title:
        raise ProjectError("An issue needs a title.")
    if priority not in set(TaskPriority):
        raise ProjectError(f"priority must be one of: {', '.join(TaskPriority)}")

    issue = ProjectIssue(
        project_id=project.id,
        position=len(project.issues),
        title=title,
        detail=detail,
        priority=priority,
        owner_id=owner_id,
        raised_by_id=actor.id,
        raised_on=_today(),
        due_on=due_on,
        needs_support=needs_support,
        support_note=support_note,
    )
    session.add(issue)
    await session.flush()
    project.issues.append(issue)

    _log(
        session, project,
        kind=UpdateKind.ISSUE, actor=actor, subject=title, issue_id=issue.id,
        status_after=IssueStatus.OPEN, body=detail,
    )
    return issue


async def update_issue(
    session: AsyncSession,
    project: Project,
    issue: ProjectIssue,
    *,
    actor: User,
    **changes: Any,
) -> ProjectIssue:
    allowed = {
        "title", "detail", "status", "priority", "owner_id", "due_on",
        "resolved_on", "needs_support", "support_note", "position",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ProjectError(f"Cannot change: {', '.join(sorted(unknown))}")
    if "status" in changes and changes["status"] not in set(IssueStatus):
        raise ProjectError(f"status must be one of: {', '.join(IssueStatus)}")
    if "priority" in changes and changes["priority"] not in set(TaskPriority):
        raise ProjectError(f"priority must be one of: {', '.join(TaskPriority)}")

    was_status = issue.status
    for key, value in changes.items():
        setattr(issue, key, value)

    # Closing an issue dates it, and reopening one clears the date. Neither is
    # something anybody should have to remember, and a resolved_on left behind
    # on a reopened issue is what makes an "issues closed this month" count
    # quietly wrong.
    if was_status != issue.status:
        if issue.status not in OPEN_ISSUE_STATUSES and issue.resolved_on is None:
            issue.resolved_on = _today()
        elif issue.status in OPEN_ISSUE_STATUSES:
            issue.resolved_on = None
        _log(
            session, project,
            kind=UpdateKind.ISSUE, actor=actor, subject=issue.title, issue_id=issue.id,
            status_before=was_status, status_after=issue.status,
        )

    await session.flush()
    return issue


async def delete_issue(session: AsyncSession, project: Project, issue: ProjectIssue) -> None:
    """Removed from the collection; ``delete-orphan`` issues the DELETE."""
    project.issues[:] = [i for i in project.issues if i.id != issue.id]
    await session.flush()


# ── the progress log ───────────────────────────────────────────────────


def _log(
    session: AsyncSession,
    project: Project,
    *,
    kind: str,
    actor: User | None,
    subject: str | None = None,
    task_id: uuid.UUID | None = None,
    milestone_id: uuid.UUID | None = None,
    issue_id: uuid.UUID | None = None,
    percent_before: int | None = None,
    percent_after: int | None = None,
    status_before: str | None = None,
    status_after: str | None = None,
    hours: Decimal | None = None,
    body: str | None = None,
) -> ProjectUpdate:
    """Append one entry to the log. Synchronous: it only adds to the session.

    Not a coroutine on purpose, so the operations above can record what they
    did inline without an await interrupting the change they are making — and
    so no caller can accidentally forget to await the record of their own work.
    """
    row = ProjectUpdate(
        project_id=project.id,
        task_id=task_id,
        milestone_id=milestone_id,
        issue_id=issue_id,
        author_id=actor.id if actor else None,
        kind=kind,
        subject=(subject or "")[:500] or None,
        percent_before=percent_before,
        percent_after=percent_after,
        status_before=status_before,
        status_after=status_after,
        hours=hours,
        body=body,
    )
    session.add(row)
    return row


async def add_note(
    session: AsyncSession, project: Project, *, actor: User, body: str
) -> ProjectUpdate:
    """A progress update that is only words. The plainest thing a member can file."""
    body = (body or "").strip()
    if not body:
        raise ProjectError("An update needs something in it.")
    row = _log(
        session, project, kind=UpdateKind.NOTE, actor=actor,
        subject=project.name, body=body,
    )
    await session.flush()
    return row


async def log_between(
    session: AsyncSession,
    *,
    project_ids: Sequence[uuid.UUID],
    since: date,
    until: date,
    kinds: Sequence[str] | None = None,
    author_id: uuid.UUID | None = None,
    limit: int = 500,
) -> list[ProjectUpdate]:
    """Everything that moved on these projects between two dates, inclusive.

    The one query behind every windowed view in the module: a day's stand-up, a
    week's report, a year's retrospective. ``until`` is inclusive of the whole
    day, which is why the upper bound is built from the day after rather than
    from ``until`` itself — a report on "today" that stopped at midnight this
    morning would be empty every time.
    """
    if not project_ids:
        return []

    from datetime import time, timedelta

    lower = datetime.combine(since, time.min, tzinfo=UTC)
    upper = datetime.combine(until + timedelta(days=1), time.min, tzinfo=UTC)

    query = (
        select(ProjectUpdate)
        .where(
            ProjectUpdate.project_id.in_(project_ids),
            ProjectUpdate.created_at >= lower,
            ProjectUpdate.created_at < upper,
        )
        .order_by(ProjectUpdate.created_at.desc())
        .limit(limit)
    )
    if kinds:
        query = query.where(ProjectUpdate.kind.in_(kinds))
    if author_id is not None:
        query = query.where(ProjectUpdate.author_id == author_id)
    return list((await session.scalars(query)).all())


# ── reading them ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Summary:
    """One project plus everything countable about it, for a list or a board."""

    project: Project
    rollup: progress.Rollup
    schedule_hint: progress.Suggestion
    cost_hint: progress.Suggestion
    stale: bool


async def summaries(
    session: AsyncSession,
    viewer: Viewer,
    *,
    team_id: uuid.UUID | None = None,
    statuses: Sequence[str] | None = None,
    include_archived: bool = False,
    mine_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Summary], int]:
    """The projects this person may see, each with its figures worked out.

    **The rows are loaded and counted in Python rather than aggregated in SQL,
    and that is a deliberate trade.** Every number here — percent complete,
    what counts as overdue, which tasks count at all — is a rule defined once in
    ``app.projects.progress`` and used by the board, the API and every report.
    A second implementation of those rules in SQL would be faster and would
    eventually disagree with the first, and a portfolio page whose percentages
    differ from the project page's is worse than a slower one.

    The cost is bounded by paging: at most ``limit`` projects, whose tasks and
    issues arrive as two batched queries however many projects there are.
    """
    base = select(Project)
    if not include_archived:
        base = base.where(Project.archived_at.is_(None))
    if team_id is not None:
        base = base.where(Project.team_id == team_id)
    if statuses:
        base = base.where(Project.status.in_(statuses))
    if mine_only:
        # Their own projects, whether they lead them or merely hold work in
        # them. Deliberately not widened by team oversight: a team lead asking
        # for "mine" means the ones they are on, not the forty their team runs.
        mine = [Project.lead_id == viewer.user_id]
        if viewer.member_of:
            mine.append(Project.id.in_(viewer.member_of))
        base = base.where(or_(*mine))
    base = visible(base, viewer)

    total = await session.scalar(
        select(func.count()).select_from(base.order_by(None).subquery())
    )

    rows = list(
        (
            await session.scalars(
                _loaded(base)
                .order_by(Project.status, Project.target_end_on.asc().nulls_last(), Project.name)
                .limit(limit)
                .offset(offset)
            )
        ).unique().all()
    )

    today, now = _today(), _now()
    return [
        Summary(
            project=project,
            rollup=progress.rollup(
                project, project.tasks, project.milestones, project.issues, today
            ),
            schedule_hint=progress.schedule_suggestion(
                project.milestones, project.tasks, today
            ),
            cost_hint=progress.cost_suggestion(project),
            stale=progress.health_is_stale(project, now),
        )
        for project in rows
    ], int(total or 0)


def summarise(project: Project) -> Summary:
    """The same figures for one already-loaded project.

    Shares ``summaries``' definition of every number by calling the same
    functions, so the detail page and the list it was reached from cannot show
    different percentages for the same project.
    """
    today, now = _today(), _now()
    return Summary(
        project=project,
        rollup=progress.rollup(project, project.tasks, project.milestones, project.issues, today),
        schedule_hint=progress.schedule_suggestion(project.milestones, project.tasks, today),
        cost_hint=progress.cost_suggestion(project),
        stale=progress.health_is_stale(project, now),
    )


async def my_tasks(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    open_only: bool = True,
    due_before: date | None = None,
    limit: int = 200,
) -> list[ProjectTask]:
    """One person's work across every project they are on.

    Ordered by date with the undated last, because a task with no deadline is
    not urgent — it is unplanned, and it belongs at the bottom of the list
    rather than at the top where a null would otherwise sort it.
    """
    query = (
        select(ProjectTask)
        .options(selectinload(ProjectTask.project))
        .where(ProjectTask.assignee_id == user_id)
        .order_by(ProjectTask.due_on.asc().nulls_last(), ProjectTask.priority)
        .limit(limit)
    )
    if open_only:
        query = query.where(ProjectTask.status.in_(OPEN_TASK_STATUSES))
    if due_before is not None:
        query = query.where(ProjectTask.due_on <= due_before)
    return list((await session.scalars(query)).unique().all())


async def visible_project_ids(
    session: AsyncSession, viewer: Viewer, *, team_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Just the ids, for the windowed log and for reports covering everything."""
    query = select(Project.id).where(Project.archived_at.is_(None))
    if team_id is not None:
        query = query.where(Project.team_id == team_id)
    return list((await session.scalars(visible(query, viewer))).all())


@dataclass(frozen=True, slots=True)
class Portfolio:
    """The whole readable set at a glance — the top of a manager's dashboard."""

    projects: int
    by_status: dict[str, int]
    by_rag: dict[str, int]
    tasks_open: int
    tasks_overdue: int
    issues_open: int
    issues_needing_support: int
    milestones_overdue: int
    average_percent: int
    stale_health: int


async def portfolio(
    session: AsyncSession, viewer: Viewer, *, team_id: uuid.UUID | None = None
) -> Portfolio:
    """Roll every readable project into one set of figures.

    Narrowed to what the caller may read, exactly as the listing is. An
    ordinary member asking for this gets a portfolio of their own projects
    rather than a refusal — the same courtesy the reports overview extends.
    """
    rows, _ = await summaries(session, viewer, team_id=team_id, limit=500)

    by_status: dict[str, int] = {}
    by_rag: dict[str, int] = {}
    for row in rows:
        by_status[row.project.status] = by_status.get(row.project.status, 0) + 1
        by_rag[row.project.rag_overall] = by_rag.get(row.project.rag_overall, 0) + 1

    return Portfolio(
        projects=len(rows),
        by_status=by_status,
        by_rag=by_rag,
        tasks_open=sum(r.rollup.tasks_open for r in rows),
        tasks_overdue=sum(r.rollup.tasks_overdue for r in rows),
        issues_open=sum(r.rollup.issues_open for r in rows),
        issues_needing_support=sum(r.rollup.issues_needing_support for r in rows),
        milestones_overdue=sum(r.rollup.milestones_overdue for r in rows),
        average_percent=(
            int(round(sum(r.rollup.percent_complete for r in rows) / len(rows))) if rows else 0
        ),
        stale_health=sum(1 for r in rows if r.stale),
    )
