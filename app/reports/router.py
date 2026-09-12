"""Reports over HTTP.

One surface for two callers. A person clicking through the page and the
assistant acting on somebody's behalf hit exactly these routes, with that
person's own session, and are refused by exactly the same code. That is what
makes "ask the AI to file my report" a question about who is asking rather than
about what the assistant is allowed to be.

Every gate is in ``app.reports.access`` and every rule in ``app.reports.service``.
Nothing here decides anything on its own; it translates HTTP into those and
their refusals back into status codes. A reader wanting to know who can see
what should read ``access.py`` and be done.

One thing worth naming: a report somebody may not read comes back **404, not
403**. A 403 on a report id confirms that a report exists for that team on that
day, which is itself something they are not entitled to know.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import service as access_service
from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.mail import MailError
from app.models.project import OPEN_TASK_STATUSES, ProjectUpdate, TaskStatus
from app.assistant import service as assistant_service
from app.assistant.service import AssistantError
from app.models.report import (
    BriefFollowup,
    BriefMode,
    DeliveryStatus,
    Report,
    ReportScope,
    ReportStatus,
)
from app.models.templates import FormTemplate
from app.models.user import User
from app.projects import service as projects_service
from app.projects.access import may_report_on
from app.projects.progress import milestone_percent, milestone_state
from app.projects.service import ProjectError
from app.proposals.sharepoint import (
    SharePointConsentError,
    SharePointError,
    SharePointProposals,
)
from app.reports import service
from app.reports.access import (
    COMPANY_WIDE,
    Viewer,
    may_comment,
    may_delete,
    may_edit,
    may_file_for,
)
from app.reports.catalogue import (
    COMPLETIONS,
    PROJECT_SCHEDULE_DEFAULTS,
    period_for,
    period_label,
    scope_of,
    sections_for,
)
from app.reports import export
from app.reports.brief import Briefer
from app.reports.mailer import ReportMailer
from app.reports.schemas import (
    BriefChatOut,
    BriefOut,
    CommentIn,
    CommentOut,
    DeliveryOut,
    DeliveryPage,
    IssueOut,
    MetricOut,
    OverviewOut,
    ProjectChoiceOut,
    ProjectLineOut,
    ReportEditIn,
    ReportFieldOut,
    ReportFormOut,
    ReportOut,
    ReportPage,
    ReportSettingsIn,
    ReportSettingsOut,
    ReportStartIn,
    ReportSummaryOut,
    ScheduleIn,
    ScheduleOut,
    SectionOut,
    TaskLineOut,
    TemplateChoiceOut,
)
from app.reports.service import (
    IssueInput,
    ReportConflictError,
    ReportError,
    ReportNotFoundError,
    ReportPermissionError,
    TaskInput,
)
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles
from app.teams import service as teams_service
from app.teams.service import TeamError

logger = logging.getLogger("hamdaz.reports")

router = APIRouter(prefix="/reports", tags=["reports"])
#: Setting reports up is a separate surface from filing them, and separate for
#: the same reason the assistant's is: deciding what every team must report is a
#: narrower question than running a team. Registered ahead of ``router`` so
#: ``/reports/admin/...`` is matched before ``/reports/{report_id}``.
admin_router = APIRouter(prefix="/reports/admin", tags=["reports admin"])

Session = Annotated[AsyncSession, Depends(get_session)]

MODULE_KEY = "reports"


def get_sharepoint(request: Request) -> SharePointProposals:
    return request.app.state.sharepoint


def get_mailer(request: Request) -> ReportMailer:
    return request.app.state.report_mailer


def get_briefer(request: Request) -> Briefer:
    return request.app.state.report_briefer


Mailer = Annotated[ReportMailer, Depends(get_mailer)]
Writer = Annotated[Briefer, Depends(get_briefer)]
Config = Annotated[Settings, Depends(get_settings)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    """Only a super admin decides what a team is asked to report.

    Deliberately narrower than the admin role used elsewhere: a manager reads
    every report, which is a different power from rewriting the questions
    everybody answers.
    """
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can set up report templates and schedules.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


def _translate(exc: ReportError) -> HTTPException:
    if isinstance(exc, ReportNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ReportPermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, ReportConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def require_module(user: CurrentUser, roles: CurrentRoles, session: Session) -> None:
    """The caller reaches reports by a team grant, or by running the company.

    The second half is not a loophole, it is the module's shape. Filing a report
    is ordinary team work, so the module has to be team-grantable — which means
    the grant is held through membership. But the people the reports are *for*
    are exactly the ones who are on no team: a CEO reads every team's reports
    and belongs to none of them, and making them join all of them to read what
    is written about them would be absurd.

    Nothing is widened by letting them in. This gate answers "may you use this
    feature at all"; what any of them can actually see is decided afterwards by
    ``app.reports.access``, which still gives a CEO nobody's drafts and gives an
    ordinary person only their own. Filing is narrower still — ``may_file_for``
    wants real membership, so a CEO admitted here cannot file for a team they
    are not on.
    """
    if not COMPANY_WIDE.isdisjoint(set(roles)):
        return
    if not await access_service.can_reach(
        session, user_id=user.id, global_roles=roles, module_key=MODULE_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your team does not have the Reports module. "
                "A super admin can grant it."
            ),
        )


async def current_viewer(user: CurrentUser, session: Session) -> Viewer:
    return await service.build_viewer(session, user)


CurrentViewer = Annotated[Viewer, Depends(current_viewer)]
ModuleGate = Annotated[None, Depends(require_module)]


# ── rendering ──────────────────────────────────────────────────────────


def _sections(template: FormTemplate) -> list[SectionOut]:
    """The sections *this* template has, not every section that exists.

    A presales daily gets the standard six; a project status report gets health
    dials and a milestone timeline instead of some of them. Resolved from the
    template rather than from a constant, because the whole point of project
    reporting is that selecting a different team changes the shape of the form.
    """
    return [
        SectionOut(key=s.key, name=s.name, description=s.description, kind=s.kind)
        for s in sections_for(template)
    ]


def _fields(template: FormTemplate) -> list[ReportFieldOut]:
    return [
        ReportFieldOut(
            key=f.get("key", ""),
            label=f.get("label", ""),
            type=f.get("type", "text"),
            section=f.get("section", "metrics"),
            required=bool(f.get("required")),
            help=f.get("help"),
            options=f.get("options"),
        )
        for f in (template.fields or [])
        if isinstance(f, dict) and f.get("key")
    ]


def _summary_out(report: Report, viewer: Viewer, *, read: bool = False) -> ReportSummaryOut:
    return ReportSummaryOut(
        id=report.id,
        team_id=report.team_id,
        team=report.team.name,
        author_id=report.author_id,
        author_name=report.author.display_name,
        cadence=report.cadence,
        period_start=report.period_start,
        period_end=report.period_end,
        period_label=period_label(report.cadence, report.period_start, report.period_end),
        scope=report.scope,
        project_id=report.project_id,
        # Read off the report's own snapshot rather than the live project, so a
        # listing still names a project that has since been deleted.
        project_name=(
            report.project_lines[0].name
            if report.scope == ReportScope.PROJECT and report.project_lines
            else None
        ),
        status=report.status,
        submitted_at=report.submitted_at,
        task_count=len(report.tasks),
        open_issue_count=sum(1 for i in report.issues if not i.resolved),
        read_by_me=read,
    )


def _report_out(
    report: Report,
    viewer: Viewer,
    template: FormTemplate,
    comments: list[Any],
    *, read: bool = False,
) -> ReportOut:
    base = _summary_out(report, viewer, read=read)
    return ReportOut(
        **base.model_dump(),
        template_id=report.template_id,
        template_name=template.name,
        template_version=report.template_version,
        overview=report.overview,
        remarks=report.remarks,
        summary=report.summary,
        answers=report.answers or {},
        sections=_sections(template),
        fields=_fields(template),
        tasks=[TaskLineOut.model_validate(t) for t in report.tasks],
        issues=[IssueOut.model_validate(i) for i in report.issues],
        project_lines=[ProjectLineOut.model_validate(p) for p in report.project_lines],
        metrics=[
            MetricOut(
                key=m.key, label=m.label, unit=m.unit, computed=m.computed,
                value=m.value, target=m.target, effective=m.effective, edited=m.edited,
            )
            for m in report.metrics
        ],
        comments=[
            CommentOut(
                id=c.id,
                author_id=c.author_id,
                author_name=c.author.display_name,
                body=c.body,
                created_at=c.created_at,
            )
            for c in comments
        ],
        can_edit=may_edit(report, viewer),
        can_submit=may_edit(report, viewer),
        can_comment=may_comment(report, viewer),
        can_delete=may_delete(report, viewer),
    )


# ── what will be asked ─────────────────────────────────────────────────


@router.get(
    "/form",
    response_model=ReportFormOut,
    summary="What this team's report asks, before filling one in",
)
async def report_form(
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
    team: Annotated[str, Query(description="Team handle (slug) or id")],
    cadence: Annotated[str, Query(description="daily, weekly, monthly or ad_hoc")] = "daily",
    on: Annotated[date | None, Query(description="A day inside the period.")] = None,
) -> ReportFormOut:
    """The sections and the team's own questions, before anybody starts typing.

    Served separately from starting a draft so a page can show what is coming —
    and so the assistant can tell somebody what it is about to ask them.
    """
    try:
        found = await teams_service.get_team(session, team)
    except TeamError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such team"
        ) from exc
    if not may_file_for(found.id, viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not on that team.",
        )
    try:
        template = await service.template_for(session, team_id=found.id, cadence=cadence)
    except ReportError as exc:
        raise _translate(exc) from exc

    start, end = period_for(cadence, on or datetime.now(UTC).date())
    scope = scope_of(template)

    # A project-scoped form has to offer a choice of project, because the very
    # first thing it asks is which one. Offered rather than left to the caller
    # to guess at, and narrowed twice over: only projects on this team, and
    # only ones this person may actually report on.
    choices: list[ProjectChoiceOut] = []
    if scope == ReportScope.PROJECT:
        choices = await _project_choices(
            session, user, team_id=found.id, cadence=cadence, period_start=start
        )

    return ReportFormOut(
        team_id=found.id,
        team=found.name,
        cadence=cadence,
        template_id=template.id,
        template_name=template.name,
        template_version=template.version,
        scope=scope,
        projects=choices,
        sections=_sections(template),
        fields=_fields(template),
        completions=list(COMPLETIONS),
        period_start=start,
        period_end=end,
        period_label=period_label(cadence, start, end),
    )


async def _project_choices(
    session: AsyncSession,
    user: User,
    *,
    team_id: uuid.UUID,
    cadence: str,
    period_start: date,
) -> list[ProjectChoiceOut]:
    """The projects this person may file a status report on for this team.

    Two narrowings, and both matter. ``summaries`` already limits the list to
    projects they may *read*; ``may_report_on`` narrows it again to the ones
    they run, because filing a status report on somebody else's project would
    be reporting on work you are not answerable for.

    Each choice says whether they have already filed on it for this period, so
    the picker can grey it out rather than letting somebody fill in a whole
    report and be refused at the end.
    """
    viewer = await projects_service.build_viewer(session, user)
    rows, _total = await projects_service.summaries(
        session, viewer, team_id=team_id, limit=200
    )
    mine = [r for r in rows if may_report_on(r.project, viewer)]
    if not mine:
        return []

    taken = set(
        (
            await session.scalars(
                select(Report.project_id).where(
                    Report.author_id == user.id,
                    Report.cadence == cadence,
                    Report.period_start == period_start,
                    Report.project_id.in_([r.project.id for r in mine]),
                )
            )
        ).all()
    )
    return [
        ProjectChoiceOut(
            id=r.project.id,
            name=r.project.name,
            code=r.project.code,
            label=r.project.label,
            status=r.project.status,
            rag_overall=r.project.rag_overall,
            percent_complete=r.rollup.percent_complete,
            already_reported=r.project.id in taken,
        )
        for r in mine
    ]


# ── writing one ────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=ReportOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a report",
)
async def start_report(
    body: ReportStartIn,
    request: Request,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ReportOut:
    """Open a draft, prefilled with the caller's own Proposals tasks.

    The tasks pulled in are **theirs**, derived from their session through the
    SharePoint lookup — there is no parameter that changes whose work ends up on
    somebody's report. If the Proposals list cannot be reached the draft is still
    created, empty: a reporting tool that refuses to open because another system
    is down is a reporting tool people stop using.
    """
    if not may_file_for(body.team_id, viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only file a report for a team you are on.",
        )

    # Which template applies decides what the draft is even made of, so it is
    # resolved once here and handed to the service rather than looked up twice
    # and possibly differently.
    try:
        template = await service.template_for(
            session, team_id=body.team_id, cadence=body.cadence
        )
    except ReportError as exc:
        raise _translate(exc) from exc
    scope = scope_of(template)

    start_on, end_on = period_for(body.cadence, body.on or datetime.now(UTC).date())
    if body.period_start is not None:
        start_on = body.period_start
    if body.period_end is not None:
        end_on = body.period_end

    prefill: list[TaskInput] = []
    project_lines: list[service.ProjectLineInput] = []

    if scope == ReportScope.TEAM:
        if body.prefill_tasks:
            prefill = await _own_tasks(
                get_sharepoint(request), user.email, include_closed=body.include_closed
            )
    else:
        project_lines = await _project_prefill(
            session,
            user,
            scope=scope,
            team_id=body.team_id,
            project_id=body.project_id,
            since=start_on,
            until=end_on,
            with_milestones=body.prefill_milestones,
        )
        # A project report's task section is the project's own open work, not
        # the author's SharePoint bids — those are a different kind of task
        # belonging to a different system, and putting them on a project status
        # report would be nonsense.
        if body.prefill_tasks and scope == ReportScope.PROJECT and body.project_id:
            prefill = await _project_tasks(session, viewer_user=user, project_id=body.project_id)

    try:
        report = await service.start(
            session,
            author=user,
            team_id=body.team_id,
            cadence=body.cadence,
            on=body.on,
            period_start=body.period_start,
            period_end=body.period_end,
            prefill=prefill,
            # Passed through as sent rather than nulled for the non-project
            # scopes, so the service's own check runs and a caller naming a
            # project on a team report is told why it was refused instead of
            # having it quietly dropped.
            project_id=body.project_id,
            project_lines=project_lines,
            template=template,
        )
    except ReportError as exc:
        raise _translate(exc) from exc
    await session.commit()

    report = await service.get(session, report.id)
    template = await session.get(FormTemplate, report.template_id)
    return _report_out(report, viewer, template, [])


async def _own_tasks(
    sharepoint: SharePointProposals, email: str, *, include_closed: bool
) -> list[TaskInput]:
    """The caller's Proposals rows, as report task lines.

    Whose rows these are is derived from the session, never from the request.
    A failure to reach SharePoint returns nothing rather than raising: the draft
    is worth having without them, and the author can type what they need.
    """
    try:
        lookup_id = await sharepoint.lookup_id_for(email)
        if lookup_id is None:
            return []
        tasks = await sharepoint.tasks_assigned_to(lookup_id, limit=200)
    except (SharePointError, SharePointConsentError):
        return []
    if not include_closed:
        tasks = [t for t in tasks if t.is_open]
    tasks = sorted(tasks, key=lambda t: (t.deadline is None, t.deadline or ""))
    return service.tasks_from_proposals(tasks)


async def _project_prefill(
    session: AsyncSession,
    user: User,
    *,
    scope: str,
    team_id: uuid.UUID,
    project_id: uuid.UUID | None,
    since: date,
    until: date,
    with_milestones: bool,
) -> list[service.ProjectLineInput]:
    """Snapshot the projects a status report covers, as of right now.

    One line for a project report, one per project for a portfolio report. The
    figures are taken here and frozen — see ``ReportProjectLine`` for why a
    report that changed after it was filed would not be a report.

    The two "in period" counts are read from the project's update log between
    the report's own dates, which is what makes a weekly report about the week
    rather than about the running total. That is also the only reason the log
    exists: nothing else can answer "what moved between these two dates" once
    the tasks have moved on twice more.

    Refuses rather than silently filing an empty report when the caller may not
    report on the project they named. A status report with no project on it
    would be a blank page nobody could explain.
    """
    viewer = await projects_service.build_viewer(session, user)

    if scope == ReportScope.PROJECT:
        if project_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This report is about one project. Say which.",
            )
        try:
            project = await projects_service.get_for(session, project_id, viewer)
        except ProjectError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        if not may_report_on(project, viewer):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the project lead or somebody running the team files on it.",
            )
        if project.team_id != team_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That project belongs to a different team.",
            )
        chosen = [projects_service.summarise(project)]
    else:
        rows, _total = await projects_service.summaries(
            session, viewer, team_id=team_id, limit=200
        )
        # A portfolio report covers what this person can see of the team's
        # work. Not everything the team runs: a report listing projects its
        # author may not open would leak exactly what the access rules exist
        # to keep back.
        chosen = rows

    if not chosen:
        return []

    counts = await _movement_in_period(
        session, [r.project.id for r in chosen], since=since, until=until
    )

    today = datetime.now(UTC).date()
    lines: list[service.ProjectLineInput] = []
    for row in chosen:
        stones: list[service.MilestoneInput] = []
        if with_milestones:
            tasks = list(row.project.tasks)
            stones = [
                service.MilestoneInput(
                    milestone_id=m.id,
                    name=m.name,
                    owner_name=m.owner.display_name if m.owner else None,
                    start_on=m.start_on,
                    due_on=m.due_on,
                    done_on=m.done_on,
                    baseline_due_on=m.baseline_due_on,
                    percent_complete=milestone_percent(m, tasks),
                    plan=m.plan,
                    state=milestone_state(m, today),
                    is_key=m.is_key,
                )
                for m in row.project.milestones
            ]
        moved = counts.get(row.project.id, (0, 0))
        lines.append(
            service.project_line_from(
                row,
                milestones=stones,
                updates_in_period=moved[0],
                tasks_completed_in_period=moved[1],
            )
        )
    return lines


async def _movement_in_period(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
    *,
    since: date,
    until: date,
) -> dict[uuid.UUID, tuple[int, int]]:
    """Per project: how many updates landed in the window, and how many tasks
    were finished in it.

    Both counted from the update log rather than from the tasks themselves.
    ``done_at`` on a task would answer the second question only until the task
    was reopened and closed again, at which point last month's report would
    quietly change its mind. The log does not move.
    """
    if not project_ids:
        return {}

    lower = datetime.combine(since, time.min, tzinfo=UTC)
    upper = datetime.combine(until + timedelta(days=1), time.min, tzinfo=UTC)

    rows = (
        await session.execute(
            select(
                ProjectUpdate.project_id,
                func.count().label("updates"),
                func.count()
                .filter(ProjectUpdate.status_after == TaskStatus.DONE)
                .label("completed"),
            )
            .where(
                ProjectUpdate.project_id.in_(project_ids),
                ProjectUpdate.created_at >= lower,
                ProjectUpdate.created_at < upper,
            )
            .group_by(ProjectUpdate.project_id)
        )
    ).all()
    return {row[0]: (int(row[1]), int(row[2])) for row in rows}


async def _project_tasks(
    session: AsyncSession, *, viewer_user: User, project_id: uuid.UUID
) -> list[TaskInput]:
    """A project's open work, as report task rows.

    Ordered soonest-first with the undated last, the same convention the
    projects board uses — a task with no deadline is unplanned rather than
    urgent, and a null that sorted to the top would put it where the most
    pressing work belongs.
    """
    viewer = await projects_service.build_viewer(session, viewer_user)
    try:
        project = await projects_service.get_for(session, project_id, viewer)
    except ProjectError:
        return []

    rows = sorted(
        (t for t in project.tasks if t.status in OPEN_TASK_STATUSES),
        key=lambda t: (t.due_on is None, t.due_on or date.max),
    )
    return [
        TaskInput(
            title=task.title,
            # The vocabularies are identical by design — see
            # ``models.project.TaskStatus`` — so no mapping is needed and there
            # is no value at which a mapping could be wrong.
            completion=task.status,
            source="manual",
            external_id=str(task.id),
            status=task.status,
            percent_complete=task.percent_complete,
            priority=task.priority,
            deadline=task.due_on,
            link=f"/projects/{project.id}/tasks/{task.id}",
            note=task.blocked_reason,
        )
        for task in rows[:200]
    ]


@router.patch("/{report_id}", response_model=ReportOut, summary="Fill in a draft")
async def edit_report(
    report_id: uuid.UUID,
    body: ReportEditIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ReportOut:
    """Only the fields sent change. A list sent at all replaces that section."""
    try:
        report = await service.get_for(session, report_id, viewer)
    except ReportError as exc:
        raise _translate(exc) from exc
    # Two different refusals, and telling them apart is the whole point.
    # Somebody else's report is a 403 — they may not touch it, ever. Their own
    # filed report is a 409 — they could have, and the moment has passed. One
    # of those is worth explaining and the other is worth apologising for.
    if report.author_id != viewer.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only its author can change a report.",
        )
    try:
        await service.edit(
            session,
            report,
            overview=body.overview,
            remarks=body.remarks,
            summary=body.summary,
            answers=body.answers,
            tasks=(
                [TaskInput(**row.model_dump()) for row in body.tasks]
                if body.tasks is not None
                else None
            ),
            issues=(
                [IssueInput(**row.model_dump()) for row in body.issues]
                if body.issues is not None
                else None
            ),
            metrics=body.metrics,
            project_notes=(
                {
                    str(line_id): note.model_dump(exclude_unset=True)
                    for line_id, note in body.project_notes.items()
                }
                if body.project_notes
                else None
            ),
        )
    except ReportError as exc:
        raise _translate(exc) from exc
    await session.commit()

    report = await service.get(session, report_id)
    template = await session.get(FormTemplate, report.template_id)
    return _report_out(report, viewer, template, [])


@router.post(
    "/{report_id}/submit", response_model=ReportOut, summary="File it"
)
async def submit_report(
    report_id: uuid.UUID,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    mailer: Mailer,
    briefer: Writer,
    settings: Config,
    _: ModuleGate,
) -> ReportOut:
    """After this it is read-only, and the people it goes to can see it.

    Who that is: the team's managers and leads, plus the CEO, any global
    manager, and super admins. Nothing is sent anywhere — they read it where it
    lives, which is why there is no notification to fail silently.
    """
    try:
        report = await service.get_for(session, report_id, viewer)
    except ReportError as exc:
        raise _translate(exc) from exc
    # Same split as editing: not yours is a 403, already filed is a 409.
    if report.author_id != viewer.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only its author can submit a report.",
        )
    try:
        await service.submit(session, report)
    except ReportError as exc:
        raise _translate(exc) from exc
    await session.commit()

    # Written here under ``on_submit``, which is the mode that costs nobody any
    # waiting: the author is already waiting on the email, and every manager
    # who opens the report afterwards finds the short version already there.
    # Like the email, it never fails the filing — the report is filed.
    brief_settings = await service.get_settings(session)
    if (
        brief_settings.brief_enabled
        and brief_settings.brief_mode == BriefMode.ON_SUBMIT
    ):
        await _ensure_brief(session, report, brief_settings, briefer, user=user)
        await session.commit()

    await _notify(session, report, mailer, settings)

    report = await service.get(session, report_id)
    template = await session.get(FormTemplate, report.template_id)
    return _report_out(report, viewer, template, [])


def _report_link(report: Report, settings: Settings) -> str:
    return f"{settings.frontend_url.rstrip('/')}/reports/{report.id}"


async def _notify(
    session: AsyncSession,
    report: Report,
    mailer: ReportMailer,
    settings: Settings,
) -> None:
    """Mail the filed report to the people it goes to. **Never fails the filing.**

    A report that is submitted is submitted whether or not the mail went. But a
    silent failure hides the one fact that matters — that nobody was told — so
    what went wrong is kept on the report itself, where somebody can find it
    while asking about that particular report.
    """
    if not settings.notify_by_email:
        return

    rules = await service.get_settings(session)
    plan = await service.plan_delivery(session, report, rules)
    if plan.skipped is not None:
        # Recorded rather than left blank. "Why did my manager not get it" is
        # the question this log exists to answer, and a missing row answers it
        # with a shrug.
        await service.record_delivery(
            session, report,
            status=DeliveryStatus.SKIPPED, addresses=[], detail=plan.skipped,
        )
        await session.commit()
        return

    try:
        await mailer.send_submitted(
            report, plan.addresses, link=_report_link(report, settings), rules=rules
        )
    except MailError as exc:
        await service.record_delivery(
            session, report,
            status=DeliveryStatus.FAILED, addresses=plan.addresses, detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - a notification never fails the filing
        # Logged as well as recorded. The catch-all is right — a report that is
        # filed is filed whether or not the mail went — but it will happily
        # swallow a programming error and file it under "delivery failed",
        # which reads as an infrastructure problem and gets ignored. The
        # traceback is the difference between that and a bug somebody fixes.
        logger.exception("report %s: could not send the notification", report.id)
        await service.record_delivery(
            session, report,
            status=DeliveryStatus.FAILED,
            addresses=plan.addresses,
            detail=f"{type(exc).__name__}: {exc}",
        )
    else:
        await service.record_delivery(
            session, report,
            status=DeliveryStatus.SENT, addresses=plan.addresses, detail=None,
        )
    await session.commit()


@router.delete(
    "/{report_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a report"
)
async def delete_report(
    report_id: uuid.UUID,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> None:
    try:
        report = await service.get_for(session, report_id, viewer)
    except ReportError as exc:
        raise _translate(exc) from exc
    if not may_delete(report, viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A submitted report can only be removed by a super admin.",
        )
    await service.delete(session, report)
    await session.commit()


# ── reading them ───────────────────────────────────────────────────────


@router.get("", response_model=ReportPage, summary="Reports I can see")
async def list_reports(
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
    team: Annotated[str | None, Query(description="Team handle or id")] = None,
    author_id: Annotated[uuid.UUID | None, Query()] = None,
    cadence: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
    mine: Annotated[bool, Query(description="Only my own.")] = False,
    scope: Annotated[
        str | None, Query(description="team, project or portfolio")
    ] = None,
    project_id: Annotated[
        uuid.UUID | None, Query(description="Status reports on one project")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReportPage:
    """Newest period first, narrowed to what this person may read.

    Every filter narrows; none of them widens. Somebody asking for a team whose
    reports they cannot read gets an empty list rather than a refusal, because
    the honest answer is that there are none they can see.
    """
    team_id = None
    if team is not None:
        try:
            team_id = (await teams_service.get_team(session, team)).id
        except TeamError:
            return ReportPage(reports=[], total=0)

    rows, total = await service.listing(
        session,
        viewer,
        team_id=team_id,
        author_id=author_id,
        cadence=cadence,
        status=status_filter,
        since=since,
        until=until,
        mine_only=mine,
        scope=scope,
        project_id=project_id,
        limit=limit,
        offset=offset,
    )
    return ReportPage(
        reports=[_summary_out(r, viewer) for r in rows], total=total
    )


@router.get("/overview", response_model=OverviewOut, summary="What the reports say together")
async def reports_overview(
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
    team: Annotated[str | None, Query(description="Team handle or id")] = None,
    since: Annotated[date | None, Query(description="Defaults to 30 days ago.")] = None,
    until: Annotated[date | None, Query(description="Defaults to today.")] = None,
) -> OverviewOut:
    """The figures and the open issues across every report the caller may read.

    This is the route a CEO or a manager asks the assistant for — "how did
    presales do last month", "what is blocking us". It is narrowed by the same
    rule as everything else, so an ordinary person asking gets their own
    reports summarised and nobody else's.
    """
    today = datetime.now(UTC).date()
    end = until or today
    start = since or (end - timedelta(days=29))
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'since' must not be after 'until'",
        )
    team_id = None
    if team is not None:
        try:
            team_id = (await teams_service.get_team(session, team)).id
        except TeamError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such team"
            ) from None
    return OverviewOut.model_validate(
        await service.overview(session, viewer, since=start, until=end, team_id=team_id)
    )


@router.get("/{report_id}", response_model=ReportOut, summary="One report in full")
async def read_report(
    report_id: uuid.UUID,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> ReportOut:
    """Reading somebody else's submitted report records that you read it.

    Recorded because the complaint reports always attract is the same one —
    "nobody reads them" — and this is how that gets answered with a fact rather
    than an impression. Reading your own is not recorded; it would mean nothing.
    """
    try:
        report = await service.get_for(session, report_id, viewer)
    except ReportError as exc:
        raise _translate(exc) from exc

    read = False
    if report.author_id != viewer.user_id and report.status == ReportStatus.SUBMITTED:
        await service.mark_read(session, report=report, user_id=viewer.user_id)
        await session.commit()
        read = True

    template = await session.get(FormTemplate, report.template_id)
    comments = await service.comments(session, report.id)
    return _report_out(report, viewer, template, comments, read=read)


@router.post(
    "/{report_id}/comments",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Comment on a report",
)
async def comment_on_report(
    report_id: uuid.UUID,
    body: CommentIn,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
) -> CommentOut:
    """A reader's remark. It decides nothing and changes nothing about the report.

    The author cannot comment on their own: what they have to add belongs in the
    remarks section, or in the next report. Letting them append after filing
    would make "what did they report on Tuesday" unanswerable.
    """
    try:
        report = await service.get_for(session, report_id, viewer)
    except ReportError as exc:
        raise _translate(exc) from exc
    if not may_comment(report, viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only a reader of a submitted report can comment on it. "
                "Its author has the remarks section."
            ),
        )
    try:
        comment = await service.add_comment(
            session, report=report, author=user, body=body.body
        )
    except ReportError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return CommentOut(
        id=comment.id,
        author_id=comment.author_id,
        author_name=user.display_name,
        body=comment.body,
        created_at=comment.created_at,
    )


@router.get(
    "/{report_id}/export",
    summary="This report as a file",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "The report"}},
)
async def export_report(
    report_id: uuid.UUID,
    session: Session,
    viewer: CurrentViewer,
    _: ModuleGate,
    fmt: Annotated[str, Query(alias="format", description="pdf or docx")] = "pdf",
) -> Response:
    """The filed report, to attach to something or paste into something.

    A copy of the report, so it is shown to exactly the people who may read the
    report — the same 404 for everybody else, since whether a report exists for
    that team on that day is itself not theirs to learn.

    Both formats come from one description of the document, so neither can
    quietly stop carrying a section the other has. See ``app.reports.export``.
    """
    try:
        report = await service.get_for(session, report_id, viewer)
    except ReportError as exc:
        raise _translate(exc) from exc

    try:
        content, name, media_type = export.render(report, fmt.lower().strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return Response(
        content=content,
        media_type=media_type,
        headers={
            # `attachment` rather than `inline`: this is a file somebody asked
            # for, and a PDF that opens in a browser tab instead of landing in
            # Downloads is one they then have to save by hand.
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


# ── the brief ──────────────────────────────────────────────────────────
#
# A report is long because a record should be. A manager with nine of them to
# read on a Monday needs the short version first and the long one when the
# short one worries them, which is what these three routes are.
#
# None of them is a new permission. A brief is made of one report's contents,
# so it is shown to exactly the people who may read that report and refused —
# as a 404, like the report itself — to everybody else.


def _brief_out(report: Report, settings: Any) -> BriefOut:
    state = service.brief_state(report, settings)
    followup = settings.brief_followup
    usable = state in ("ready", "stale")
    return BriefOut(
        report_id=report.id,
        state=state,
        headline=report.brief_headline,
        body=report.brief,
        generated_at=report.brief_generated_at,
        model=report.brief_model,
        revision=report.brief_revision or 0,
        error=report.brief_error,
        # A brief that does not exist cannot be chatted about, and one the
        # administrator has switched follow-ups off for cannot be either. Both
        # are said here rather than left for the page to work out, so the page
        # and the endpoint cannot disagree about which buttons exist.
        may_refresh=(
            state in ("absent", "failed", "stale", "ready")
            and followup in (BriefFollowup.REFRESH, BriefFollowup.CHAT)
            and settings.brief_enabled
            and report.status == ReportStatus.SUBMITTED
        ),
        may_chat=usable and followup == BriefFollowup.CHAT,
    )


async def _ensure_brief(
    session: AsyncSession,
    report: Report,
    settings: Any,
    briefer: Briefer,
    *,
    user: User,
    force: bool = False,
) -> None:
    """Write the brief if it is wanted and not already there.

    Failures are swallowed into ``brief_error`` on the report rather than
    raised. Every caller of this is doing something else that matters more —
    filing a report, opening a page — and none of them should fail because a
    summary could not be written.
    """
    assistant_settings = await assistant_service.get_settings(session)
    try:
        await service.write_brief(
            session,
            report,
            briefer=briefer,
            settings=settings,
            model_key=service.brief_model_key(settings, assistant_settings.model_key),
            user_key=str(user.id),
            force=force,
        )
    except ReportError as exc:
        logger.warning("brief for report %s could not be written: %s", report.id, exc)


@router.get(
    "/{report_id}/brief",
    response_model=BriefOut,
    summary="The short version of this report",
)
async def read_brief(
    report_id: uuid.UUID,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    briefer: Writer,
    _: ModuleGate,
) -> BriefOut:
    """What the report says, in a paragraph.

    Under ``on_first_open`` this is where the brief actually gets written, and
    the first manager to open the report is the one who waits for it. Every
    reader after them is served the stored one — a submitted report never
    changes, so writing it twice would buy an identical paragraph.
    """
    try:
        report = await service.get_for(session, report_id, viewer)
    except ReportError as exc:
        raise _translate(exc) from exc

    settings = await service.get_settings(session)
    if (
        settings.brief_enabled
        and settings.brief_mode == BriefMode.ON_FIRST_OPEN
        and report.status == ReportStatus.SUBMITTED
        and not report.brief
        and not report.brief_error
    ):
        await _ensure_brief(session, report, settings, briefer, user=user)
        await session.commit()
    return _brief_out(report, settings)


@router.post(
    "/{report_id}/brief",
    response_model=BriefOut,
    summary="Write it again",
)
async def refresh_brief(
    report_id: uuid.UUID,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    briefer: Writer,
    _: ModuleGate,
) -> BriefOut:
    """Ask for another brief on the same report.

    Not a cache bust: the report has not changed, the reader simply wants it
    said again, usually because the first one was too short or missed what they
    care about. It costs a model call every time, which is why the
    administrator can switch it off with ``brief_followup``.

    This one *does* report its failures. Somebody pressed a button and is
    waiting; telling them nothing happened is worse than telling them why.
    """
    try:
        report = await service.get_for(session, report_id, viewer)
    except ReportError as exc:
        raise _translate(exc) from exc

    settings = await service.get_settings(session)
    if settings.brief_followup == BriefFollowup.OFF:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Asking for another brief is switched off.",
        )

    assistant_settings = await assistant_service.get_settings(session)
    try:
        await service.write_brief(
            session,
            report,
            briefer=briefer,
            settings=settings,
            model_key=service.brief_model_key(settings, assistant_settings.model_key),
            user_key=str(user.id),
            force=True,
        )
    except ReportError as exc:
        await session.commit()  # keeps the recorded failure
        raise _translate(exc) from exc
    await session.commit()
    return _brief_out(report, settings)


@router.post(
    "/{report_id}/chat",
    response_model=BriefChatOut,
    summary="Ask the assistant about this report",
)
async def chat_about_report(
    report_id: uuid.UUID,
    user: CurrentUser,
    session: Session,
    viewer: CurrentViewer,
    briefer: Writer,
    _: ModuleGate,
) -> BriefChatOut:
    """Open the box on this report, with the brief already in it.

    Returns a real assistant conversation. Everything after this goes through
    the assistant's own endpoints, which means the follow-up questions are
    subject to the same admission rules, the same tool policies, the same cost
    caps and the same audit log as any other chat — rather than a second, less
    watched way to ask the model things.

    Called twice by the same person, it hands back the same conversation. The
    questions a manager asked about Tuesday's report are worth finding again on
    Wednesday.
    """
    try:
        report = await service.get_for(session, report_id, viewer)
    except ReportError as exc:
        raise _translate(exc) from exc

    settings = await service.get_settings(session)
    if not settings.brief_enabled or settings.brief_followup != BriefFollowup.CHAT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Asking questions about a report is switched off.",
        )

    existing = await assistant_service.conversation_about(
        session, user_id=user.id, subject_kind="report", subject_id=report.id
    )
    if existing is not None:
        await session.commit()
        return BriefChatOut(
            conversation_id=existing.id,
            created=False,
            brief=_brief_out(report, settings),
        )

    # The chat is only worth opening with something in it, so a report nobody
    # has briefed yet gets briefed here — whatever the mode says. The mode
    # decides when a brief appears *by itself*; asking for one is asking.
    if not report.brief:
        await _ensure_brief(session, report, settings, briefer, user=user)
    if not report.brief:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=report.brief_error or "The brief could not be written.",
        )

    label = (
        f"{report.team.name} {report.cadence}, "
        f"{period_label(report.cadence, report.period_start, report.period_end)}"
    )
    try:
        conversation = await assistant_service.create_conversation(
            session,
            user=user,
            title=f"Brief — {label}"[:200],
            subject_kind="report",
            subject_id=report.id,
            subject_label=label[:200],
        )
    except AssistantError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    # Seeded as an assistant message, so it is both the first thing the manager
    # reads and the first thing the model sees of this conversation. The agent
    # replays stored messages as history, which is what makes a follow-up
    # question land against the summary rather than against nothing.
    await assistant_service.add_message(
        session,
        conversation,
        role="assistant",
        content=report.brief,
        run_id=None,
    )
    await session.commit()
    return BriefChatOut(
        conversation_id=conversation.id,
        created=True,
        brief=_brief_out(report, settings),
    )


# ── setting them up ────────────────────────────────────────────────────


@admin_router.get(
    "/settings",
    response_model=ReportSettingsOut,
    summary="Who filed reports are sent to",
)
async def read_settings(admin: SuperAdmin, session: Session) -> ReportSettingsOut:
    """The delivery rules. Super admin only, like every other one here.

    Note what these do not control: who may *read* a report. Delivery and
    visibility are separate questions, and only the first is configurable.
    Adding an address below mails them a summary; the link in it refuses them
    exactly as it would anybody else who may not read that report.
    """
    return ReportSettingsOut.model_validate(await service.get_settings(session))


@admin_router.patch(
    "/settings", response_model=ReportSettingsOut, summary="Change who they go to"
)
async def update_settings(
    body: ReportSettingsIn, admin: SuperAdmin, session: Session
) -> ReportSettingsOut:
    """Only the fields sent change.

    The common change is turning dailies off and leaving weeklies on: a manager
    of six people otherwise gets thirty messages a week and reads none of them.
    """
    try:
        row = await service.update_settings(
            session, actor_id=admin.id, changes=body.model_dump(exclude_unset=True)
        )
    except ReportError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return ReportSettingsOut.model_validate(row)


@admin_router.get(
    "/deliveries",
    response_model=DeliveryPage,
    summary="What was emailed, to whom, and what failed",
)
async def delivery_log(
    admin: SuperAdmin,
    session: Session,
    since: Annotated[
        date | None, Query(description="Defaults to the retention window in settings.")
    ] = None,
    status_filter: Annotated[
        str | None, Query(alias="status", description="sent, failed or skipped")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DeliveryPage:
    """The delivery log, newest first. Super admin and nobody else.

    Restricted because of what it holds rather than what it does: who was
    mailed about whom is a map of the organisation's reporting lines, and an
    ordinary person has no business reading it. The counts come back alongside
    so a screen can say "3 failed this week" without paging through rows.
    """
    rules = await service.get_settings(session)
    window = since or (
        datetime.now(UTC).date() - timedelta(days=rules.log_retention_days)
    )
    start = datetime.combine(window, datetime.min.time(), tzinfo=UTC)
    rows, total = await service.deliveries(
        session, since=start, status=status_filter, limit=limit, offset=offset
    )
    return DeliveryPage(
        deliveries=[DeliveryOut.model_validate(r) for r in rows],
        total=total,
        counts=await service.delivery_counts(session, since=start),
    )


@admin_router.get(
    "/templates",
    response_model=list[TemplateChoiceOut],
    summary="Report templates a team can be pointed at",
)
async def list_report_templates(
    admin: SuperAdmin, session: Session
) -> list[TemplateChoiceOut]:
    return [
        TemplateChoiceOut(
            id=t.id,
            key=t.key,
            name=t.name,
            description=t.description,
            version=t.version,
            field_count=len(t.fields or []),
        )
        for t in await service.report_templates(session)
    ]


@admin_router.get(
    "/schedules",
    response_model=list[ScheduleOut],
    summary="Which template each team files",
)
async def list_schedules(admin: SuperAdmin, session: Session) -> list[ScheduleOut]:
    return [_schedule_out(s) for s in await service.schedules(session)]


def _schedule_out(row: Any) -> ScheduleOut:
    return ScheduleOut(
        id=row.id,
        team_id=row.team_id,
        team=row.team.name,
        cadence=row.cadence,
        template_id=row.template_id,
        template_name=row.template.name,
        enabled=row.enabled,
        due_hour=row.due_hour,
        due_weekday=row.due_weekday,
        note=row.note,
        notify=row.notify,
        extra_recipients=list(row.extra_recipients or []),
    )


@admin_router.put(
    "/schedules",
    response_model=ScheduleOut,
    summary="Point one team's cadence at one template",
)
async def put_schedule(
    body: ScheduleIn, admin: SuperAdmin, session: Session
) -> ScheduleOut:
    """This is what makes each team's report different from the next team's.

    Super admin only, like every other template decision: what the business
    records is not something a team quietly changes for itself.
    """
    try:
        row = await service.set_schedule(
            session,
            team_id=body.team_id,
            cadence=body.cadence,
            template_id=body.template_id,
            actor_id=admin.id,
            enabled=body.enabled,
            due_hour=body.due_hour,
            due_weekday=body.due_weekday,
            note=body.note,
            notify=body.notify,
            extra_recipients=body.extra_recipients,
        )
    except ReportError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return _schedule_out(row)


@admin_router.post(
    "/schedules/project-reporting",
    response_model=list[ScheduleOut],
    summary="Switch a team over to project status reporting",
)
async def adopt_project_reporting(
    admin: SuperAdmin,
    session: Session,
    team: Annotated[str, Query(description="Team handle (slug) or id")],
    cadences: Annotated[
        list[str] | None,
        Query(description=f"Any of: {', '.join(PROJECT_SCHEDULE_DEFAULTS)}"),
    ] = None,
) -> list[ScheduleOut]:
    """Point a team's cadences at the shipped project status templates.

    A convenience over ``PUT /schedules``, not a new power: it writes the same
    rows an administrator would write by hand, without them having to know
    which of the shipped templates goes with which cadence. Everything else on
    an existing schedule — who is copied, the note, the hour it is due — is
    carried over, because changing what a team is asked should not silently
    reset who reads the answers.

    After this, somebody on that team opening the report page for one of these
    cadences is asked which project, and gets health dials and a milestone
    timeline instead of the six standard sections. That needed no new
    mechanism: which template a team files has always been a schedule row.
    """
    try:
        found = await teams_service.get_team(session, team)
    except TeamError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such team"
        ) from exc

    try:
        rows = await service.adopt_project_reporting(
            session, team_id=found.id, actor_id=admin.id, cadences=cadences
        )
    except ReportError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return [_schedule_out(row) for row in rows]
