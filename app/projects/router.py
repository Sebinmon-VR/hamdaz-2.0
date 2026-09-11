"""Projects over HTTP.

One surface for two callers. A person clicking through the board and the
assistant acting on somebody's behalf hit exactly these routes, with that
person's own session, and are refused by exactly the same code.

Every gate is in ``app.projects.access`` and every rule in
``app.projects.service``. Nothing here decides anything on its own; it
translates HTTP into those and their refusals back into status codes.

One thing worth naming: a project somebody may not read comes back **404, not
403**. A 403 on a project id confirms that the id names a real project of some
team, which is itself something they are not entitled to know.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import service as access_service
from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.models.project import (
    OPEN_TASK_STATUSES,
    Project,
    ProjectIssue,
    ProjectMilestone,
    ProjectTask,
)
from app.models.user import User
from app.projects import service
from app.projects.access import (
    COMPANY_WIDE,
    Viewer,
    may_administer,
    may_create,
    may_manage,
    may_report_on,
    may_update_task,
)
from app.projects.progress import (
    GRAINS,
    milestone_percent,
    milestone_state,
    slip_days,
    task_is_overdue,
    window_for,
    window_label,
)
from app.projects.schemas import (
    ActivityOut,
    BoardOut,
    DialOut,
    HealthIn,
    HealthOut,
    IssueEditIn,
    IssueIn,
    IssueOut,
    MemberIn,
    MemberOut,
    MilestoneEditIn,
    MilestoneIn,
    MilestoneOut,
    MyTaskOut,
    NoteIn,
    PersonOut,
    PortfolioOut,
    ProjectEditIn,
    ProjectIn,
    ProjectOut,
    ProjectPage,
    ProjectSummaryOut,
    RollupOut,
    TaskEditIn,
    TaskIn,
    TaskOut,
    UpdateOut,
)
from app.projects.service import (
    ProjectConflictError,
    ProjectError,
    ProjectNotFoundError,
    ProjectPermissionError,
)
from app.roles.deps import CurrentRoles
from app.teams import service as teams_service
from app.teams.service import TeamError

logger = logging.getLogger("hamdaz.projects")

router = APIRouter(prefix="/projects", tags=["projects"])

Session = Annotated[AsyncSession, Depends(get_session)]

MODULE_KEY = "projects"


def _translate(exc: ProjectError) -> HTTPException:
    if isinstance(exc, ProjectNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ProjectPermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, ProjectConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def require_module(user: CurrentUser, roles: CurrentRoles, session: Session) -> None:
    """The caller reaches projects by a team grant, or by running the company.

    The second half is the same shape the reports module has, and it is not a
    loophole. Working on a project is ordinary team work, so the module has to
    be team-grantable — but the people who need the portfolio view are exactly
    the ones on no team. Nothing is widened by admitting them: this gate answers
    "may you use this feature at all", and what any of them can actually see is
    decided afterwards by ``app.projects.access``.
    """
    if not COMPANY_WIDE.isdisjoint(set(roles)):
        return
    if not await access_service.can_reach(
        session, user_id=user.id, global_roles=roles, module_key=MODULE_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your team does not have the Projects module. A super admin can grant it.",
        )


async def current_viewer(user: CurrentUser, session: Session) -> Viewer:
    return await service.build_viewer(session, user)


CurrentViewer = Annotated[Viewer, Depends(current_viewer)]
ModuleGate = Annotated[None, Depends(require_module)]


# ── rendering ──────────────────────────────────────────────────────────


def _person(user: User | None) -> PersonOut | None:
    if user is None:
        return None
    return PersonOut(id=user.id, name=user.display_name, email=user.email)


def _today() -> date:
    return datetime.now(UTC).date()


def _milestone_out(
    milestone: ProjectMilestone, tasks: list[ProjectTask], today: date
) -> MilestoneOut:
    return MilestoneOut(
        id=milestone.id,
        position=milestone.position,
        name=milestone.name,
        detail=milestone.detail,
        owner=_person(milestone.owner),
        start_on=milestone.start_on,
        due_on=milestone.due_on,
        done_on=milestone.done_on,
        baseline_due_on=milestone.baseline_due_on,
        percent_complete=milestone_percent(milestone, tasks),
        plan=milestone.plan,
        is_key=milestone.is_key,
        state=milestone_state(milestone, today),
        slip_days=slip_days(milestone),
        task_count=sum(1 for t in tasks if t.milestone_id == milestone.id),
    )


def _task_out(
    task: ProjectTask, project: Project, viewer: Viewer, today: date
) -> TaskOut:
    return TaskOut(
        id=task.id,
        project_id=task.project_id,
        milestone_id=task.milestone_id,
        position=task.position,
        title=task.title,
        detail=task.detail,
        assignee=_person(task.assignee),
        status=task.status,
        priority=task.priority,
        percent_complete=task.percent_complete,
        start_on=task.start_on,
        due_on=task.due_on,
        done_at=task.done_at,
        estimate_hours=task.estimate_hours,
        spent_hours=task.spent_hours,
        blocked_reason=task.blocked_reason,
        overdue=task_is_overdue(task, today),
        can_update=may_update_task(project, task, viewer),
    )


def _issue_out(issue: ProjectIssue, today: date) -> IssueOut:
    end = issue.resolved_on or today
    return IssueOut(
        id=issue.id,
        position=issue.position,
        title=issue.title,
        detail=issue.detail,
        status=issue.status,
        priority=issue.priority,
        owner=_person(issue.owner),
        raised_on=issue.raised_on,
        due_on=issue.due_on,
        resolved_on=issue.resolved_on,
        needs_support=issue.needs_support,
        support_note=issue.support_note,
        age_days=max(0, (end - issue.raised_on).days),
    )


#: The five dials, their labels, and which of them anything can compute. Scope
#: and benefits carry no suggestion on purpose — no arithmetic over tasks and
#: dates can tell you whether the scope has crept or whether the thing will
#: deliver what it promised, and inventing one would be worse than silence.
_DIALS: tuple[tuple[str, str, str, str], ...] = (
    ("overall", "Overall", "rag_overall", "trend_overall"),
    ("scope", "Scope", "rag_scope", "trend_scope"),
    ("cost", "Costs", "rag_cost", "trend_cost"),
    ("schedule", "Schedule", "rag_schedule", "trend_schedule"),
    ("benefits", "Benefits", "rag_benefits", "trend_benefits"),
)


def _health_out(summary: service.Summary) -> HealthOut:
    project = summary.project
    hints = {
        "schedule": summary.schedule_hint,
        "cost": summary.cost_hint,
        # Overall borrows the schedule hint: it is the one computable signal
        # that bears on the whole project, and an overall dial reading green
        # beside two overdue milestones is exactly the disagreement worth
        # surfacing. It is a hint, never a value — see DialOut.
        "overall": summary.schedule_hint,
    }
    dials = []
    for key, label, rag_attr, trend_attr in _DIALS:
        hint = hints.get(key)
        stored = getattr(project, rag_attr)
        dials.append(
            DialOut(
                key=key,
                label=label,
                rag=stored,
                trend=getattr(project, trend_attr),
                suggested=hint.rag if hint else None,
                suggested_reason=hint.reason if hint else None,
                differs=bool(hint and hint.rag != "grey" and hint.rag != stored),
            )
        )
    return HealthOut(
        dials=dials,
        reviewed_at=project.health_reviewed_at,
        reviewed_note=project.health_note,
        stale=summary.stale,
    )


def _rollup_out(rollup: Any) -> RollupOut:
    return RollupOut(
        tasks_total=rollup.tasks_total,
        tasks_done=rollup.tasks_done,
        tasks_open=rollup.tasks_open,
        tasks_blocked=rollup.tasks_blocked,
        tasks_overdue=rollup.tasks_overdue,
        milestones_total=rollup.milestones_total,
        milestones_done=rollup.milestones_done,
        milestones_overdue=rollup.milestones_overdue,
        issues_open=rollup.issues_open,
        issues_needing_support=rollup.issues_needing_support,
        percent_complete=rollup.percent_complete,
    )


def _portfolio_out(totals: service.Portfolio) -> PortfolioOut:
    """Field by field rather than by unpacking the dataclass.

    ``Portfolio`` is declared with ``slots=True`` and so has no ``__dict__`` to
    unpack — and naming the fields is what makes a field added there a
    deliberate decision here rather than something that silently appears in an
    API response.
    """
    return PortfolioOut(
        projects=totals.projects,
        by_status=totals.by_status,
        by_rag=totals.by_rag,
        tasks_open=totals.tasks_open,
        tasks_overdue=totals.tasks_overdue,
        issues_open=totals.issues_open,
        issues_needing_support=totals.issues_needing_support,
        milestones_overdue=totals.milestones_overdue,
        average_percent=totals.average_percent,
        stale_health=totals.stale_health,
    )


def _summary_out(summary: service.Summary) -> ProjectSummaryOut:
    project = summary.project
    return ProjectSummaryOut(
        id=project.id,
        team_id=project.team_id,
        team=project.team.name,
        code=project.code,
        name=project.name,
        label=project.label,
        objective=project.objective,
        status=project.status,
        lead=_person(project.lead),
        start_on=project.start_on,
        target_end_on=project.target_end_on,
        actual_end_on=project.actual_end_on,
        rag_overall=project.rag_overall,
        trend_overall=project.trend_overall,
        percent_complete=summary.rollup.percent_complete,
        currency=project.currency,
        budget_amount=project.budget_amount,
        spend_amount=project.spend_amount,
        archived=project.is_archived,
        health_stale=summary.stale,
        rollup=_rollup_out(summary.rollup),
    )


def _project_out(summary: service.Summary, viewer: Viewer) -> ProjectOut:
    project = summary.project
    today = _today()
    tasks = list(project.tasks)
    held: dict[uuid.UUID, int] = {}
    for task in tasks:
        if task.assignee_id is not None and task.status in OPEN_TASK_STATUSES:
            held[task.assignee_id] = held.get(task.assignee_id, 0) + 1

    return ProjectOut(
        **_summary_out(summary).model_dump(),
        description=project.description,
        health=_health_out(summary),
        members=[
            MemberOut(
                user_id=m.user_id,
                name=m.user.display_name,
                email=m.user.email,
                role=m.role,
                responsibility=m.responsibility,
                open_tasks=held.get(m.user_id, 0),
            )
            for m in sorted(project.members, key=lambda m: (m.role != "lead", m.user.display_name))
        ],
        milestones=[_milestone_out(m, tasks, today) for m in project.milestones],
        tasks=[_task_out(t, project, viewer, today) for t in tasks],
        issues=[_issue_out(i, today) for i in project.issues],
        can_manage=may_manage(project, viewer),
        can_administer=may_administer(project, viewer),
        can_report=may_report_on(project, viewer),
    )


def _update_out(row: Any, names: dict[uuid.UUID, str]) -> UpdateOut:
    return UpdateOut(
        id=row.id,
        project_id=row.project_id,
        project_name=names.get(row.project_id, ""),
        task_id=row.task_id,
        milestone_id=row.milestone_id,
        issue_id=row.issue_id,
        author=_person(row.author),
        kind=row.kind,
        subject=row.subject,
        percent_before=row.percent_before,
        percent_after=row.percent_after,
        percent_delta=row.percent_delta,
        status_before=row.status_before,
        status_after=row.status_after,
        hours=row.hours,
        body=row.body,
        created_at=row.created_at,
    )


# ── loading one, with its gate ─────────────────────────────────────────


async def _load(session: AsyncSession, project_id: uuid.UUID, viewer: Viewer) -> Project:
    try:
        return await service.get_for(session, project_id, viewer)
    except ProjectError as exc:
        raise _translate(exc) from exc


def _require_manage(project: Project, viewer: Viewer) -> None:
    if not may_manage(project, viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the project lead or somebody running the team can change the plan.",
        )


# ── the board ──────────────────────────────────────────────────────────


@router.get("/board", response_model=BoardOut, summary="My work and my projects")
async def board(
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
    team: Annotated[str | None, Query(description="Narrow to one team")] = None,
) -> BoardOut:
    """The landing page: what is on this person's plate, and where it lives.

    Served as one call rather than three because it is one screen, and three
    round trips to draw a dashboard is how a dashboard gets a reputation for
    being slow. Every figure in it is narrowed to what this caller may read.
    """
    team_id = await _team_id(session, team) if team else None
    today = _today()

    tasks = await service.my_tasks(session, user_id=user.id)
    rows, _total = await service.summaries(
        session, viewer, team_id=team_id, mine_only=not viewer.is_company_wide, limit=50
    )
    totals = await service.portfolio(session, viewer, team_id=team_id)

    week_end = today + timedelta(days=7)
    return BoardOut(
        my_open_tasks=len(tasks),
        my_overdue_tasks=sum(1 for t in tasks if task_is_overdue(t, today)),
        my_due_this_week=sum(
            1 for t in tasks if t.due_on is not None and today <= t.due_on <= week_end
        ),
        tasks=[
            MyTaskOut(
                **_task_out(t, t.project, viewer, today).model_dump(),
                project_name=t.project.name,
                project_code=t.project.code,
            )
            for t in tasks
        ],
        projects=[_summary_out(r) for r in rows],
        portfolio=_portfolio_out(totals),
    )


@router.get("/portfolio", response_model=PortfolioOut, summary="Every project at a glance")
async def portfolio_view(
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
    team: Annotated[str | None, Query(description="Narrow to one team")] = None,
) -> PortfolioOut:
    team_id = await _team_id(session, team) if team else None
    return _portfolio_out(await service.portfolio(session, viewer, team_id=team_id))


@router.get("/my-tasks", response_model=list[MyTaskOut], summary="My tasks across projects")
async def my_tasks(
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
    open_only: Annotated[bool, Query()] = True,
    due_before: Annotated[date | None, Query()] = None,
) -> list[MyTaskOut]:
    today = _today()
    tasks = await service.my_tasks(
        session, user_id=user.id, open_only=open_only, due_before=due_before
    )
    return [
        MyTaskOut(
            **_task_out(t, t.project, viewer, today).model_dump(),
            project_name=t.project.name,
            project_code=t.project.code,
        )
        for t in tasks
    ]


# ── the windowed view: day, week, month, quarter, year ──────────────────


@router.get("/activity", response_model=ActivityOut, summary="What moved over a period")
async def activity(
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
    grain: Annotated[str, Query(description=f"One of: {', '.join(GRAINS)}")] = "week",
    on: Annotated[date | None, Query(description="Any day inside the period")] = None,
    since: Annotated[date | None, Query(description="custom only")] = None,
    until: Annotated[date | None, Query(description="custom only")] = None,
    team: Annotated[str | None, Query()] = None,
    project_id: Annotated[uuid.UUID | None, Query(description="One project, or all")] = None,
    kind: Annotated[
        list[str] | None, Query(description="task, health, milestone, issue, note")
    ] = None,
    mine_only: Annotated[bool, Query(description="Only my own updates")] = False,
) -> ActivityOut:
    """The progress log over a window — the whole of day/week/month/year
    reporting, expressed once.

    Day, week, month, quarter and year all resolve to two dates and then run
    the same query; ``custom`` takes the two dates directly. That is why there
    is one endpoint here rather than four, and why adding a grain later is a
    line in ``progress.window_for`` rather than a new route.
    """
    if grain not in GRAINS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"grain must be one of: {', '.join(GRAINS)}",
        )

    anchor = on or _today()
    start, end = window_for(grain, anchor)
    if grain == "custom":
        if since is None or until is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A custom period needs both 'since' and 'until'.",
            )
        start, end = since, until
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The period ends before it starts.",
        )

    if project_id is not None:
        project = await _load(session, project_id, viewer)
        ids = [project.id]
        names = {project.id: project.name}
    else:
        team_id = await _team_id(session, team) if team else None
        ids = await service.visible_project_ids(session, viewer, team_id=team_id)
        rows, _n = await service.summaries(
            session, viewer, team_id=team_id, limit=500
        )
        names = {r.project.id: r.project.name for r in rows}

    updates = await service.log_between(
        session,
        project_ids=ids,
        since=start,
        until=end,
        kinds=kind,
        author_id=viewer.user_id if mine_only else None,
    )

    counts: dict[str, int] = {}
    for row in updates:
        counts[row.kind] = counts.get(row.kind, 0) + 1

    return ActivityOut(
        since=start,
        until=end,
        grain=grain,
        label=window_label(grain, start, end),
        projects=len(ids),
        updates=[_update_out(u, names) for u in updates],
        counts=counts,
    )


# ── listing and creating ───────────────────────────────────────────────


async def _team_id(session: AsyncSession, ref: str) -> uuid.UUID:
    try:
        return (await teams_service.get_team(session, ref)).id
    except TeamError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such team"
        ) from exc


@router.get("", response_model=ProjectPage, summary="Projects I can see")
async def list_projects(
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
    team: Annotated[str | None, Query(description="Team handle (slug) or id")] = None,
    status_in: Annotated[list[str] | None, Query(alias="status")] = None,
    mine_only: Annotated[bool, Query(description="Only projects I am on")] = False,
    include_archived: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProjectPage:
    team_id = await _team_id(session, team) if team else None
    rows, total = await service.summaries(
        session,
        viewer,
        team_id=team_id,
        statuses=status_in,
        mine_only=mine_only,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return ProjectPage(projects=[_summary_out(r) for r in rows], total=total)


@router.post(
    "",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a project",
)
async def create_project(
    body: ProjectIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ProjectOut:
    """Only somebody running the team may start one.

    Oversight rather than membership, unlike filing a report: a project commits
    other people's time, so it sits with the team lead, the team manager, or
    whoever runs the company.
    """
    if not may_create(body.team_id, viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a team lead, team manager or an admin can start a project.",
        )
    try:
        await teams_service.get_team(session, body.team_id)
    except TeamError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such team"
        ) from exc

    try:
        project = await service.create(
            session, author=user, **body.model_dump()
        )
    except ProjectError as exc:
        raise _translate(exc) from exc

    # Reloaded so the response carries the same eagerly-loaded shape every
    # other read does. The creator is a member from this moment, so their
    # viewer is stale by one row — rebuilt rather than patched, because a
    # hand-patched permission set is one that eventually diverges.
    fresh = await service.build_viewer(session, user)
    return _project_out(service.summarise(await service.get(session, project.id)), fresh)


@router.get("/{project_id}", response_model=ProjectOut, summary="One project in full")
async def read_project(
    project_id: uuid.UUID,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ProjectOut:
    project = await _load(session, project_id, viewer)
    return _project_out(service.summarise(project), viewer)


@router.patch("/{project_id}", response_model=ProjectOut, summary="Change a project")
async def edit_project(
    project_id: uuid.UUID,
    body: ProjectEditIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ProjectOut:
    project = await _load(session, project_id, viewer)
    _require_manage(project, viewer)
    try:
        await service.update(
            session, project, actor=user, **body.model_dump(exclude_unset=True)
        )
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _project_out(service.summarise(await service.get(session, project.id)), viewer)


@router.put("/{project_id}/health", response_model=ProjectOut, summary="Assess the dials")
async def set_health(
    project_id: uuid.UUID,
    body: HealthIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ProjectOut:
    """Record the lead's judgement of the five dials, and stamp when.

    Its own endpoint rather than part of the patch above, because the timestamp
    is the point: saving a project's description must not claim its health was
    reassessed, and confirming the dials unchanged must count as a review.
    """
    project = await _load(session, project_id, viewer)
    _require_manage(project, viewer)
    payload = body.model_dump(exclude_unset=True)
    note = payload.pop("note", None)
    try:
        await service.set_health(session, project, actor=user, note=note, **payload)
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _project_out(service.summarise(await service.get(session, project.id)), viewer)


@router.post("/{project_id}/archive", response_model=ProjectOut, summary="Archive a project")
async def archive_project(
    project_id: uuid.UUID,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ProjectOut:
    project = await _load(session, project_id, viewer)
    if not may_administer(project, viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only somebody running the team can archive a project.",
        )
    await service.archive(session, project)
    return _project_out(service.summarise(project), viewer)


@router.post("/{project_id}/restore", response_model=ProjectOut, summary="Restore a project")
async def restore_project(
    project_id: uuid.UUID,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ProjectOut:
    project = await _load(session, project_id, viewer)
    if not may_administer(project, viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only somebody running the team can restore a project.",
        )
    await service.restore(session, project)
    return _project_out(service.summarise(project), viewer)


@router.delete(
    "/{project_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a project"
)
async def delete_project(
    project_id: uuid.UUID,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> None:
    """Removes the project and everything under it. Reports already filed
    against it survive — their project line holds a copy of what it said."""
    project = await _load(session, project_id, viewer)
    if not may_administer(project, viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only somebody running the team can delete a project.",
        )
    await service.delete(session, project)


# ── members ────────────────────────────────────────────────────────────


@router.put(
    "/{project_id}/members", response_model=ProjectOut, summary="Add or change a member"
)
async def put_member(
    project_id: uuid.UUID,
    body: MemberIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ProjectOut:
    project = await _load(session, project_id, viewer)
    _require_manage(project, viewer)
    try:
        await service.set_member(
            session,
            project,
            user_id=body.user_id,
            role=body.role,
            responsibility=body.responsibility,
            actor=user,
        )
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _project_out(service.summarise(await service.get(session, project.id)), viewer)


@router.delete(
    "/{project_id}/members/{user_id}",
    response_model=ProjectOut,
    summary="Take somebody off a project",
)
async def remove_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ProjectOut:
    """Refused while they still hold open work here — the lead decides where it
    goes rather than it being silently unassigned."""
    project = await _load(session, project_id, viewer)
    _require_manage(project, viewer)
    try:
        await service.remove_member(session, project, user_id=user_id)
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _project_out(service.summarise(await service.get(session, project.id)), viewer)


# ── milestones ─────────────────────────────────────────────────────────


@router.post(
    "/{project_id}/milestones",
    response_model=MilestoneOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a milestone",
)
async def add_milestone(
    project_id: uuid.UUID,
    body: MilestoneIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> MilestoneOut:
    project = await _load(session, project_id, viewer)
    _require_manage(project, viewer)
    try:
        row = await service.add_milestone(session, project, actor=user, **body.model_dump())
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _milestone_out(row, list(project.tasks), _today())


@router.patch(
    "/{project_id}/milestones/{milestone_id}",
    response_model=MilestoneOut,
    summary="Change a milestone",
)
async def edit_milestone(
    project_id: uuid.UUID,
    milestone_id: uuid.UUID,
    body: MilestoneEditIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> MilestoneOut:
    project = await _load(session, project_id, viewer)
    _require_manage(project, viewer)
    row = next((m for m in project.milestones if m.id == milestone_id), None)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such milestone"
        )
    try:
        await service.update_milestone(
            session, project, row, actor=user, **body.model_dump(exclude_unset=True)
        )
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _milestone_out(row, list(project.tasks), _today())


@router.delete(
    "/{project_id}/milestones/{milestone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a milestone",
)
async def delete_milestone(
    project_id: uuid.UUID,
    milestone_id: uuid.UUID,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> None:
    """The tasks underneath it stay — deleting a milestone is a re-plan, and
    the work below it is usually the reason for it."""
    project = await _load(session, project_id, viewer)
    _require_manage(project, viewer)
    row = next((m for m in project.milestones if m.id == milestone_id), None)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such milestone"
        )
    await service.delete_milestone(session, project, row)


# ── tasks ──────────────────────────────────────────────────────────────


@router.post(
    "/{project_id}/tasks",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a task",
)
async def add_task(
    project_id: uuid.UUID,
    body: TaskIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> TaskOut:
    """Whoever it is assigned to is put on the project and told about it."""
    project = await _load(session, project_id, viewer)
    _require_manage(project, viewer)
    try:
        task = await service.add_task(session, project, actor=user, **body.model_dump())
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _task_out(task, project, viewer, _today())


@router.patch(
    "/{project_id}/tasks/{task_id}", response_model=TaskOut, summary="Update a task"
)
async def edit_task(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    body: TaskEditIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> TaskOut:
    """The operation an ordinary member is allowed on their own work.

    An assignee may move percentage, status, hours and the blocked reason, and
    the change is written to the progress log either way. What they may not do
    is hand the task to somebody else — that is a planning decision, and it is
    refused here rather than silently ignored so nobody thinks it worked.
    """
    project = await _load(session, project_id, viewer)
    task = next((t for t in project.tasks if t.id == task_id), None)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such task")

    if not may_update_task(project, task, viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="That task is not yours, and you do not run this project.",
        )

    payload = body.model_dump(exclude_unset=True)
    if not may_manage(project, viewer):
        reserved = {"assignee_id", "milestone_id", "position", "title", "due_on", "priority"}
        overreach = reserved & set(payload)
        if overreach:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Only the project lead can change "
                    f"{', '.join(sorted(overreach))}. You can report progress on this task."
                ),
            )

    note = payload.pop("note", None)
    hours = payload.pop("hours", None)
    try:
        await service.update_task(
            session, project, task, actor=user, note=note, hours=hours, **payload
        )
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _task_out(task, project, viewer, _today())


@router.delete(
    "/{project_id}/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a task",
)
async def delete_task(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> None:
    project = await _load(session, project_id, viewer)
    _require_manage(project, viewer)
    task = next((t for t in project.tasks if t.id == task_id), None)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such task")
    await service.delete_task(session, project, task)


# ── issues ─────────────────────────────────────────────────────────────


@router.post(
    "/{project_id}/issues",
    response_model=IssueOut,
    status_code=status.HTTP_201_CREATED,
    summary="Raise an issue",
)
async def add_issue(
    project_id: uuid.UUID,
    body: IssueIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> IssueOut:
    """Anybody on the project may raise one.

    Deliberately wider than managing the plan: the person who trips over a
    blocker is usually not the person running the project, and a module where
    only the lead can say something is wrong is a module that finds out late.
    """
    project = await _load(session, project_id, viewer)
    try:
        issue = await service.add_issue(session, project, actor=user, **body.model_dump())
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _issue_out(issue, _today())


@router.patch(
    "/{project_id}/issues/{issue_id}", response_model=IssueOut, summary="Change an issue"
)
async def edit_issue(
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    body: IssueEditIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> IssueOut:
    project = await _load(session, project_id, viewer)
    issue = next((i for i in project.issues if i.id == issue_id), None)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such issue")
    # Its owner or the person who raised it may work it; anybody else needs to
    # run the project. Closing somebody else's issue is a judgement about work
    # that is not yours.
    if not (
        may_manage(project, viewer)
        or issue.owner_id == viewer.user_id
        or issue.raised_by_id == viewer.user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="That issue is not yours, and you do not run this project.",
        )
    try:
        await service.update_issue(
            session, project, issue, actor=user, **body.model_dump(exclude_unset=True)
        )
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _issue_out(issue, _today())


@router.delete(
    "/{project_id}/issues/{issue_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an issue",
)
async def delete_issue(
    project_id: uuid.UUID,
    issue_id: uuid.UUID,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> None:
    project = await _load(session, project_id, viewer)
    _require_manage(project, viewer)
    issue = next((i for i in project.issues if i.id == issue_id), None)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such issue")
    await service.delete_issue(session, project, issue)


# ── the progress log ───────────────────────────────────────────────────


@router.post(
    "/{project_id}/updates",
    response_model=UpdateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Post a progress update",
)
async def add_note(
    project_id: uuid.UUID,
    body: NoteIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> UpdateOut:
    """A written update with no numbers attached.

    Open to anybody on the project, because it is the plainest way for somebody
    to say what happened — and because a status report that can quote what the
    people doing the work actually said is worth more than one that cannot.
    """
    project = await _load(session, project_id, viewer)
    try:
        row = await service.add_note(session, project, actor=user, body=body.body)
    except ProjectError as exc:
        raise _translate(exc) from exc
    return _update_out(row, {project.id: project.name})


@router.get(
    "/{project_id}/updates",
    response_model=list[UpdateOut],
    summary="This project's progress log",
)
async def project_updates(
    project_id: uuid.UUID,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[UpdateOut]:
    project = await _load(session, project_id, viewer)
    today = _today()
    rows = await service.log_between(
        session,
        project_ids=[project.id],
        since=since or (today - timedelta(days=30)),
        until=until or today,
        limit=limit,
    )
    return [_update_out(r, {project.id: project.name}) for r in rows]
