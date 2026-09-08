"""Reports as database operations: drafting one, filling it in, filing it.

The rules that matter live here rather than in the router, because they have to
hold however the change arrives — the page, the API, or the assistant acting on
somebody's behalf. The assistant reaches reports through the same routes and is
refused by the same code, which is what makes "can the AI do this for me" a
question about the person rather than about the assistant.

Three properties this file is responsible for:

* a report is filled in against the template it was started with, and stays
  readable against that version afterwards;
* a submitted report never changes — not its prose, not its tasks, not its
  figures, and not by anybody;
* nothing here writes to SharePoint. Task rows are read from the Proposals list
  and snapshotted; the list is a source, never a destination.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Iterable

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.report import (
    DeliveryStatus,
    IssueSeverity,
    Report,
    ReportCadence,
    ReportComment,
    ReportDelivery,
    ReportIssue,
    ReportMetric,
    ReportRead,
    ReportSchedule,
    ReportSettings,
    ReportStatus,
    ReportTaskLine,
    TaskSource,
)
from app.models.role import Role, UserRole
from app.models.team import Team, TeamMembership
from app.models.templates import FormTemplate, TemplateStatus
from app.models.user import User
from app.reports.access import (
    COMPANY_WIDE,
    TEAM_OVERSIGHT,
    Viewer,
    may_read,
    readable_team_ids,
)
from app.reports.catalogue import (
    COMPLETIONS,
    COMPUTED_METRICS,
    REPORT_KIND,
    TEMPLATES,
    compute,
    period_for,
    period_label,
    section_specs,
)
from app.roles.service import global_role_keys
from app.teams import service as teams_service


class ReportError(Exception):
    """An operation was refused. The message is safe to show a person."""


class ReportNotFoundError(ReportError):
    pass


class ReportPermissionError(ReportError):
    pass


class ReportConflictError(ReportError):
    pass


# ── the caller ─────────────────────────────────────────────────────────


async def build_viewer(session: AsyncSession, user: User) -> Viewer:
    """Everything the access rules need about the caller, gathered once.

    ``oversees`` is the narrow set — teams where they hold a role that comes
    with reading other people's work — and not every team they are on. Being a
    member of presales does not make a colleague's report yours to read.
    """
    roles = set(await global_role_keys(session, user.id))
    memberships = await teams_service.teams_for_user(session, user.id)
    return Viewer(
        user_id=user.id,
        roles=frozenset(roles),
        team_ids=frozenset(team.id for team, _ in memberships),
        oversees=frozenset(
            team.id
            for team, keys in memberships
            if not TEAM_OVERSIGHT.isdisjoint(keys)
        ),
    )


# ── templates and schedules ────────────────────────────────────────────


async def seed_templates(session: AsyncSession) -> list[FormTemplate]:
    """Create the shipped report templates. Idempotent; admin edits survive.

    Owned by this module rather than by the form catalogue, and that direction
    matters: reports are a *consumer* of templates. Putting report definitions
    in ``app.forms`` would make the generic template machinery depend on one of
    the things built on top of it, which is the shape that turns into a circular
    import the first time either side grows.

    They ship with no grants. A grant makes a template appear in somebody's
    "forms you can fill in" list, and these are not reached that way — which
    team files which report is the schedule, not a grant.
    """
    existing = {
        t.key: t
        for t in (
            await session.scalars(
                select(FormTemplate).where(FormTemplate.kind == REPORT_KIND)
            )
        ).all()
    }
    for spec in TEMPLATES:
        if spec["key"] in existing:
            continue  # an admin owns it now
        template = FormTemplate(
            key=spec["key"],
            name=spec["name"],
            kind=REPORT_KIND,
            description=spec.get("description"),
            fields=spec["fields"],
            sections=section_specs(),
            status=TemplateStatus.ACTIVE,
        )
        template.grants = []
        session.add(template)
        existing[spec["key"]] = template
    await session.flush()
    return list(existing.values())


async def report_templates(session: AsyncSession) -> list[FormTemplate]:
    """Every active report template, for an administrator choosing one."""
    return list(
        (
            await session.scalars(
                select(FormTemplate)
                .where(
                    FormTemplate.kind == REPORT_KIND,
                    FormTemplate.status == TemplateStatus.ACTIVE,
                )
                .order_by(FormTemplate.name)
            )
        ).all()
    )


async def schedules(
    session: AsyncSession, *, team_id: uuid.UUID | None = None
) -> list[ReportSchedule]:
    query = select(ReportSchedule).order_by(ReportSchedule.cadence)
    if team_id is not None:
        query = query.where(ReportSchedule.team_id == team_id)
    return list((await session.scalars(query)).all())


async def get_schedule(
    session: AsyncSession, *, team_id: uuid.UUID, cadence: str
) -> ReportSchedule | None:
    return await session.scalar(
        select(ReportSchedule).where(
            ReportSchedule.team_id == team_id,
            ReportSchedule.cadence == cadence,
            ReportSchedule.enabled.is_(True),
        )
    )


async def set_schedule(
    session: AsyncSession,
    *,
    team_id: uuid.UUID,
    cadence: str,
    template_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    enabled: bool = True,
    due_hour: int = 18,
    due_weekday: int | None = None,
    note: str | None = None,
    notify: bool | None = None,
    extra_recipients: list[str] | None = None,
) -> ReportSchedule:
    """Point one team's cadence at one template. This is what makes each team's
    report different from the next team's."""
    if cadence not in set(ReportCadence):
        raise ReportError(f"cadence must be one of: {', '.join(ReportCadence)}")
    template = await session.get(FormTemplate, template_id)
    if template is None or template.kind != REPORT_KIND:
        raise ReportNotFoundError("No report template with that id")
    if template.status != TemplateStatus.ACTIVE:
        raise ReportConflictError(
            f"{template.name} is {template.status}; publish it before scheduling it"
        )
    if await session.get(Team, team_id) is None:
        raise ReportNotFoundError("No team with that id")

    row = await session.scalar(
        select(ReportSchedule).where(
            ReportSchedule.team_id == team_id, ReportSchedule.cadence == cadence
        )
    )
    if row is None:
        row = ReportSchedule(team_id=team_id, cadence=cadence)
        session.add(row)
    row.template_id = template_id
    row.enabled = enabled
    row.due_hour = due_hour
    row.due_weekday = due_weekday
    row.note = note
    # Null is a real value here — "follow the global setting" — so it is only
    # written when the caller said something, rather than defaulting to a no.
    row.notify = notify
    row.extra_recipients = _clean_emails(extra_recipients)
    row.created_by_id = actor_id
    await session.flush()
    return row


async def template_for(
    session: AsyncSession, *, team_id: uuid.UUID, cadence: str
) -> FormTemplate:
    """The template this team files this cadence against.

    Falls back to the generic report when a team has no schedule, so a team can
    file the day it is created rather than waiting on an administrator. The
    fallback is a real template with the six sections and no extra questions —
    not a special case in the code, which would then need testing separately.
    """
    schedule = await get_schedule(session, team_id=team_id, cadence=cadence)
    if schedule is not None:
        return schedule.template
    generic = await session.scalar(
        select(FormTemplate)
        .where(
            FormTemplate.kind == REPORT_KIND,
            FormTemplate.key == REPORT_KIND,
            FormTemplate.status == TemplateStatus.ACTIVE,
        )
        .order_by(FormTemplate.version.desc())
    )
    if generic is None:
        raise ReportNotFoundError(
            "No report template is set up. A super admin can publish one."
        )
    return generic


# ── filling one in ─────────────────────────────────────────────────────


def _fields_of(template: FormTemplate) -> list[dict[str, Any]]:
    return [f for f in (template.fields or []) if isinstance(f, dict)]


def validate_answers(
    template: FormTemplate, answers: dict[str, Any], *, require_required: bool
) -> dict[str, Any]:
    """Check the team's own questions against the template.

    Only the template's fields survive; anything else the caller sent is
    dropped rather than stored, so a stale frontend or a creative model cannot
    quietly widen what a report holds. Required fields are enforced on submit
    and not while drafting — a draft that refuses to save because a box is empty
    is a draft nobody keeps.
    """
    known = {f["key"]: f for f in _fields_of(template) if f.get("key")}
    clean: dict[str, Any] = {}
    missing: list[str] = []
    for key, spec in known.items():
        value = answers.get(key)
        if value in (None, "", []):
            if require_required and spec.get("required"):
                missing.append(str(spec.get("label") or key))
            continue
        clean[key] = value
    if missing:
        raise ReportError("Still needed: " + ", ".join(missing))
    return clean


def merge_answers(
    template: FormTemplate, stored: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Fold one edit into the answers already given.

    Merged rather than replaced, unlike the task and issue lists. Those are
    sections a caller holds in full and sends back in full; answers are
    individual questions, and somebody filling a long form over two saves — or
    an assistant that has just learnt one figure — would otherwise wipe
    everything they had already put.

    A key sent with nothing in it clears that answer, which is the only way to
    take one back once given. A key the template does not ask for is dropped.
    """
    known = {f["key"] for f in _fields_of(template) if f.get("key")}
    out = {k: v for k, v in (stored or {}).items() if k in known}
    for key, value in (incoming or {}).items():
        if key not in known:
            continue
        if value in (None, "", []):
            out.pop(key, None)
        else:
            out[key] = value
    return out


@dataclass(slots=True)
class TaskInput:
    """One task row as a caller gives it, from a form or from the assistant."""

    title: str
    completion: str = "in_progress"
    source: str = TaskSource.MANUAL
    external_id: str | None = None
    status: str | None = None
    percent_complete: int | None = None
    priority: str | None = None
    end_user: str | None = None
    quote_no: str | None = None
    deadline: date | None = None
    link: str | None = None
    attachments_url: str | None = None
    has_attachments: bool = False
    note: str | None = None


def _check_completion(value: str) -> str:
    if value not in COMPLETIONS:
        raise ReportError(f"completion must be one of: {', '.join(COMPLETIONS)}")
    return value


def _percent(value: int | None) -> int | None:
    if value is None:
        return None
    if not 0 <= value <= 100:
        raise ReportError("percent_complete must be between 0 and 100")
    return value


def tasks_from_proposals(tasks: Iterable[Any]) -> list[TaskInput]:
    """Turn Proposals rows into task inputs, snapshotting what they said.

    A snapshot rather than a reference, and that is the important part. The
    Proposals list is live: a task renamed or reassigned next month would
    silently rewrite what somebody reported this month, and a report that
    changes after it is filed is not a report. The links stay live, so anybody
    who wants the current state is one click away from it.
    """
    out: list[TaskInput] = []
    for task in tasks:
        deadline = task.closing_date
        out.append(
            TaskInput(
                title=task.title,
                completion="done" if not task.is_open else "in_progress",
                source=TaskSource.PROPOSALS,
                external_id=str(task.id),
                status=task.effective_status or task.status,
                priority=task.priority,
                end_user=task.end_user,
                quote_no=task.quote_no,
                deadline=deadline,
                link=task.web_url,
                attachments_url=task.attachments_url,
                has_attachments=bool(task.has_attachments),
            )
        )
    return out


def _apply_tasks(report: Report, rows: list[TaskInput]) -> None:
    report.tasks.clear()
    for position, row in enumerate(rows):
        report.tasks.append(
            ReportTaskLine(
                position=position,
                source=row.source,
                external_id=row.external_id,
                title=row.title.strip()[:500] or "(untitled)",
                status=row.status,
                completion=_check_completion(row.completion),
                percent_complete=_percent(row.percent_complete),
                priority=row.priority,
                end_user=row.end_user,
                quote_no=row.quote_no,
                deadline=row.deadline,
                link=row.link,
                attachments_url=row.attachments_url,
                has_attachments=row.has_attachments,
                note=row.note,
            )
        )


@dataclass(slots=True)
class IssueInput:
    title: str
    detail: str | None = None
    severity: str = IssueSeverity.MEDIUM
    waiting_on: str | None = None
    resolved: bool = False


def _apply_issues(report: Report, rows: list[IssueInput]) -> None:
    report.issues.clear()
    for position, row in enumerate(rows):
        if row.severity not in set(IssueSeverity):
            raise ReportError(f"severity must be one of: {', '.join(IssueSeverity)}")
        report.issues.append(
            ReportIssue(
                position=position,
                title=row.title.strip()[:300] or "(untitled)",
                detail=row.detail,
                severity=row.severity,
                waiting_on=row.waiting_on,
                resolved=row.resolved,
            )
        )


#: The metric keys this module works out for itself. Anything else in an
#: overrides dict is one the team types, and is created on demand.
COMPUTED_KEYS: frozenset[str] = frozenset(m.key for m in COMPUTED_METRICS)


#: The scale the metric columns store at. Values are quantized to it on the way
#: in so that a figure reads the same before and after it has been round-tripped
#: through the database — otherwise the response to the save says "2" and every
#: response after it says "2.00", and a frontend comparing the two decides the
#: number changed.
_SCALE: Final = Decimal("0.01")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(_SCALE)
    except (InvalidOperation, ValueError) as exc:
        raise ReportError(f"{value!r} is not a number") from exc


def refresh_metrics(report: Report, *, overrides: dict[str, Any] | None = None) -> None:
    """Recompute the figures from the report's own task rows.

    Computed from the report's tasks rather than from SharePoint directly,
    because the author decides which tasks the report is about — they may drop a
    row that is not really theirs, or add one the list has never heard of. A
    figure that disagreed with the list of tasks printed directly above it would
    be read as a bug whatever it was actually measuring.

    An author's correction survives a recompute. That is the whole point of
    keeping both numbers: the computed one moves as the rows change, and what
    the person put stays put until they change it.
    """
    overrides = overrides or {}
    computed = compute(report.tasks)
    existing = {m.key: m for m in report.metrics}

    for position, spec in enumerate(COMPUTED_METRICS):
        row = existing.get(spec.key)
        if row is None:
            row = ReportMetric(key=spec.key, label=spec.label, unit=spec.unit)
            report.metrics.append(row)
            existing[spec.key] = row
        row.position = position
        row.label = spec.label
        row.unit = spec.unit
        counted = computed.get(spec.key)
        row.computed = None if counted is None else counted.quantize(_SCALE)
        if spec.key in overrides:
            row.value = _decimal(overrides[spec.key])

    # A metric the template asks for that nothing can compute: the team types it.
    extra = {k: v for k, v in overrides.items() if k not in COMPUTED_KEYS}
    for offset, (key, value) in enumerate(sorted(extra.items())):
        row = existing.get(key)
        if row is None:
            row = ReportMetric(key=key, label=key.replace("_", " ").title())
            report.metrics.append(row)
            existing[key] = row
        row.position = len(COMPUTED_METRICS) + offset
        row.value = _decimal(value)


# ── the lifecycle ──────────────────────────────────────────────────────


async def start(
    session: AsyncSession,
    *,
    author: User,
    team_id: uuid.UUID,
    cadence: str,
    on: date | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    prefill: list[TaskInput] | None = None,
) -> Report:
    """Open a draft for one period, prefilled with whatever the tasks say.

    Refuses a second report for the same person, team and period. Two daily
    reports for the same Tuesday is a mistake every time, and catching it here
    is kinder than letting a manager read both and wonder which one is current.
    """
    if cadence not in set(ReportCadence):
        raise ReportError(f"cadence must be one of: {', '.join(ReportCadence)}")

    today = datetime.now(UTC).date()
    if cadence == ReportCadence.AD_HOC:
        start_on = period_start or on or today
        end_on = period_end or start_on
    else:
        start_on, end_on = period_for(cadence, on or today)
    if end_on < start_on:
        raise ReportError("The period ends before it starts")

    clash = await session.scalar(
        select(Report).where(
            Report.team_id == team_id,
            Report.author_id == author.id,
            Report.cadence == cadence,
            Report.period_start == start_on,
        )
    )
    if clash is not None:
        raise ReportConflictError(
            f"You already have a {cadence} report for "
            f"{period_label(cadence, start_on, end_on)}."
        )

    template = await template_for(session, team_id=team_id, cadence=cadence)
    report = Report(
        team_id=team_id,
        author_id=author.id,
        template_id=template.id,
        template_version=template.version,
        cadence=cadence,
        period_start=start_on,
        period_end=end_on,
        status=ReportStatus.DRAFT,
    )
    # Filled in *before* the report is added to the session, and the order
    # matters. Once it has been flushed it is a persistent object whose
    # collections are not loaded, so reading ``report.tasks`` to clear it emits
    # a SELECT — and doing that from synchronous code inside an async session
    # is a MissingGreenlet rather than a query. While it is still transient the
    # collections are simply empty, and the cascade inserts them with it.
    _apply_tasks(report, prefill or [])
    refresh_metrics(report)
    session.add(report)
    await session.flush()
    return report


async def edit(
    session: AsyncSession,
    report: Report,
    *,
    overview: str | None = None,
    remarks: str | None = None,
    summary: str | None = None,
    answers: dict[str, Any] | None = None,
    tasks: list[TaskInput] | None = None,
    issues: list[IssueInput] | None = None,
    metrics: dict[str, Any] | None = None,
) -> Report:
    """Change a draft. Only what is given changes.

    A submitted report is refused here rather than quietly ignored — the caller
    asked to change something and needs to know it did not happen.
    """
    if report.status != ReportStatus.DRAFT:
        raise ReportConflictError("A submitted report cannot be changed.")

    if overview is not None:
        report.overview = overview.strip() or None
    if remarks is not None:
        report.remarks = remarks.strip() or None
    if summary is not None:
        report.summary = summary.strip() or None
    if answers is not None:
        template = await session.get(FormTemplate, report.template_id)
        report.answers = merge_answers(template, report.answers or {}, answers)
    if tasks is not None:
        _apply_tasks(report, tasks)
    if issues is not None:
        _apply_issues(report, issues)

    # Always, because the task rows may have moved underneath the figures.
    refresh_metrics(report, overrides=metrics)
    await session.flush()
    return report


async def submit(session: AsyncSession, report: Report) -> Report:
    """File it. After this nothing about it changes.

    The required questions are enforced now and not while drafting: a draft that
    refuses to save because a box is empty is a draft nobody keeps.
    """
    if report.status == ReportStatus.SUBMITTED:
        raise ReportConflictError("That report has already been submitted.")
    if not (report.overview or "").strip():
        raise ReportError("A report needs an overview before it can be submitted.")

    template = await session.get(FormTemplate, report.template_id)
    report.answers = validate_answers(template, report.answers or {}, require_required=True)

    refresh_metrics(report)
    report.status = ReportStatus.SUBMITTED
    report.submitted_at = datetime.now(UTC)
    await session.flush()
    return report


async def delete(session: AsyncSession, report: Report) -> None:
    await session.delete(report)
    await session.flush()


# ── reading them ───────────────────────────────────────────────────────


def _loaded(query: Select) -> Select:
    return query.options(
        selectinload(Report.tasks),
        selectinload(Report.issues),
        selectinload(Report.metrics),
    )


async def get(session: AsyncSession, report_id: uuid.UUID) -> Report:
    report = await session.scalar(_loaded(select(Report).where(Report.id == report_id)))
    if report is None:
        raise ReportNotFoundError("No report with that id")
    return report


async def get_for(session: AsyncSession, report_id: uuid.UUID, viewer: Viewer) -> Report:
    """The report, or a not-found for somebody who may not read it.

    Not-found rather than forbidden, deliberately. A 403 on a report id confirms
    that a report exists for that team on that day, which is itself something
    the person is not entitled to know.
    """
    report = await get(session, report_id)
    if not may_read(report, viewer):
        raise ReportNotFoundError("No report with that id")
    return report


def _visible(query: Select, viewer: Viewer) -> Select:
    """Narrow a query to what this person may read.

    Their own whatever its state, plus submitted ones from the teams they run —
    or from everywhere, if they run the company.
    """
    if viewer.is_company_wide:
        return query.where(
            (Report.author_id == viewer.user_id)
            | (Report.status == ReportStatus.SUBMITTED)
        )
    teams = readable_team_ids(viewer) or frozenset()
    if not teams:
        return query.where(Report.author_id == viewer.user_id)
    return query.where(
        (Report.author_id == viewer.user_id)
        | (
            Report.team_id.in_(teams)
            & (Report.status == ReportStatus.SUBMITTED)
        )
    )


async def listing(
    session: AsyncSession,
    viewer: Viewer,
    *,
    team_id: uuid.UUID | None = None,
    author_id: uuid.UUID | None = None,
    cadence: str | None = None,
    status: str | None = None,
    since: date | None = None,
    until: date | None = None,
    mine_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Report], int]:
    """Reports this person may read, newest period first.

    Every filter narrows what they may already see; none of them widens it. A
    normal person asking for another team's reports gets an empty list rather
    than a refusal, because the honest answer to "show me presales' reports"
    from somebody who cannot read them is that there are none they can see.
    """
    query = _visible(select(Report), viewer)
    if team_id is not None:
        query = query.where(Report.team_id == team_id)
    if author_id is not None:
        query = query.where(Report.author_id == author_id)
    if mine_only:
        query = query.where(Report.author_id == viewer.user_id)
    if cadence is not None:
        query = query.where(Report.cadence == cadence)
    if status is not None:
        query = query.where(Report.status == status)
    if since is not None:
        query = query.where(Report.period_end >= since)
    if until is not None:
        query = query.where(Report.period_start <= until)

    total = await session.scalar(
        select(func.count()).select_from(query.subquery())
    )
    rows = (
        await session.scalars(
            _loaded(query)
            .order_by(Report.period_start.desc(), Report.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


# ── reading, commenting ────────────────────────────────────────────────


async def comments(session: AsyncSession, report_id: uuid.UUID) -> list[ReportComment]:
    return list(
        (
            await session.scalars(
                select(ReportComment)
                .where(ReportComment.report_id == report_id)
                .order_by(ReportComment.created_at)
            )
        ).all()
    )


async def add_comment(
    session: AsyncSession, *, report: Report, author: User, body: str
) -> ReportComment:
    text = body.strip()
    if not text:
        raise ReportError("A comment needs something in it.")
    row = ReportComment(report_id=report.id, author_id=author.id, body=text)
    session.add(row)
    await session.flush()
    return row


async def mark_read(
    session: AsyncSession, *, report: Report, user_id: uuid.UUID
) -> ReportRead:
    """Record that somebody read it. Idempotent: the first read is the one kept."""
    existing = await session.scalar(
        select(ReportRead).where(
            ReportRead.report_id == report.id, ReportRead.user_id == user_id
        )
    )
    if existing is not None:
        return existing
    row = ReportRead(report_id=report.id, user_id=user_id)
    session.add(row)
    await session.flush()
    return row


async def get_settings(session: AsyncSession) -> ReportSettings:
    """The one settings row, created on first use.

    Created rather than required so a fresh install works before anybody opens
    the settings screen — the defaults are the behaviour the module shipped
    with, so nothing changes until somebody decides it should.
    """
    row = await session.get(ReportSettings, 1)
    if row is None:
        row = ReportSettings(id=1)
        session.add(row)
        await session.flush()
    return row


def _clean_emails(values: list[str] | None) -> list[str]:
    """Lower-cased, de-duplicated, and anything without an ``@`` dropped.

    Refusing the whole save over one mistyped address would lose the other
    nine; dropping it silently would hide the mistake. Dropping it and leaving
    the rest is the compromise, and the screen shows what was kept.
    """
    out: list[str] = []
    for raw in values or []:
        address = str(raw).strip().lower()
        if "@" in address and address not in out:
            out.append(address)
    return out


async def update_settings(
    session: AsyncSession, *, actor_id: uuid.UUID | None, changes: dict[str, Any]
) -> ReportSettings:
    """Change the reporting settings. Only the keys given change."""
    row = await get_settings(session)

    for field in (
        "notify_on_submit",
        "notify_team_oversight",
        "notify_company_wide",
        "copy_author",
        "include_task_list",
        "include_issue_list",
    ):
        if changes.get(field) is not None:
            setattr(row, field, bool(changes[field]))

    if changes.get("max_tasks_in_email") is not None:
        row.max_tasks_in_email = max(0, int(changes["max_tasks_in_email"]))
    if changes.get("log_retention_days") is not None:
        row.log_retention_days = max(1, int(changes["log_retention_days"]))

    if changes.get("company_roles") is not None:
        known = {r.key for r in await session.scalars(select(Role))}
        wanted = [str(k).strip() for k in changes["company_roles"] if str(k).strip()]
        # A role key that does not exist would silently mail nobody, which is
        # the failure nobody notices until somebody asks why they stopped
        # getting reports.
        unknown = [k for k in wanted if k not in known]
        if unknown:
            raise ReportError(f"No such role: {', '.join(sorted(unknown))}")
        row.company_roles = wanted

    if changes.get("notify_cadences") is not None:
        wanted = [str(c).strip() for c in changes["notify_cadences"] if str(c).strip()]
        unknown = [c for c in wanted if c not in set(ReportCadence)]
        if unknown:
            raise ReportError(f"No such cadence: {', '.join(sorted(unknown))}")
        row.notify_cadences = wanted

    if changes.get("extra_recipients") is not None:
        row.extra_recipients = _clean_emails(changes["extra_recipients"])

    row.updated_by_id = actor_id
    await session.flush()
    return row


@dataclass(slots=True)
class Delivery:
    """What the settings say should happen to one filed report."""

    #: Addresses to mail. Empty when nothing should be sent.
    addresses: list[str]
    #: Why nothing is being sent, when nothing is. None means go ahead.
    skipped: str | None = None


async def plan_delivery(
    session: AsyncSession, report: Report, settings: ReportSettings
) -> Delivery:
    """Who this particular report goes to, under the current settings.

    Worked out in one place and returned rather than sent, so the rules can be
    tested without a mail server and so the log can record exactly what was
    decided — including the decision to send nothing, which is the one people
    ask about.

    What this does **not** do is widen who may read the report. An address in
    ``extra_recipients`` gets a summary in their inbox; the link in it will
    refuse them like any other person who may not read it. Delivery and
    visibility are separate questions and this answers only the first.
    """
    if not settings.notify_on_submit:
        return Delivery([], skipped="Report email is switched off.")
    if report.cadence not in (settings.notify_cadences or []):
        return Delivery(
            [], skipped=f"{report.cadence} reports are not set up to be emailed."
        )

    schedule = await session.scalar(
        select(ReportSchedule).where(
            ReportSchedule.team_id == report.team_id,
            ReportSchedule.cadence == report.cadence,
        )
    )
    if schedule is not None and schedule.notify is False:
        return Delivery(
            [], skipped="This team's reports of this cadence are not emailed."
        )

    people: list[User] = []
    if settings.notify_team_oversight:
        rows = await session.scalars(
            select(User)
            .join(TeamMembership, TeamMembership.user_id == User.id)
            .join(Role, Role.id == TeamMembership.role_id)
            .where(
                TeamMembership.team_id == report.team_id,
                Role.key.in_(TEAM_OVERSIGHT),
            )
        )
        people += list(rows.all())
    if settings.notify_company_wide and settings.company_roles:
        rows = await session.scalars(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.key.in_(settings.company_roles))
        )
        people += list(rows.all())

    addresses: list[str] = []
    for person in people:
        if person.id == report.author_id and not settings.copy_author:
            continue
        address = (person.email or "").strip().lower()
        if address and address not in addresses:
            addresses.append(address)

    if settings.copy_author and report.author and report.author.email:
        author_address = report.author.email.strip().lower()
        if author_address not in addresses:
            addresses.append(author_address)

    for address in [
        *_clean_emails(settings.extra_recipients),
        *_clean_emails(schedule.extra_recipients if schedule else []),
    ]:
        if address not in addresses:
            addresses.append(address)

    if not addresses:
        return Delivery(
            [],
            skipped=(
                "Nobody to send it to: the team has no manager or lead, and no "
                "other recipient is configured."
            ),
        )
    return Delivery(sorted(addresses))


async def recipients(session: AsyncSession, report: Report) -> list[str]:
    """The addresses this report would be mailed to right now."""
    return (await plan_delivery(session, report, await get_settings(session))).addresses


async def record_delivery(
    session: AsyncSession,
    report: Report,
    *,
    status: str,
    addresses: list[str],
    detail: str | None,
) -> ReportDelivery:
    """Note what happened to the email, on the report and in the log.

    Both, and for different readers. The columns on the report answer "did my
    manager get Tuesday's?"; the log answers "has anything failed to send this
    week?", which no per-row summary can. The log copies the names off the
    report so it still reads after that report is deleted — a delivery record
    whose subject has vanished is exactly the one somebody is looking up.
    """
    if status == DeliveryStatus.SENT:
        report.notified_at = datetime.now(UTC)
        report.notified_count = len(addresses)
        report.notify_error = None
    elif detail is not None:
        report.notify_error = detail[:2000]

    row = ReportDelivery(
        report_id=report.id,
        team_name=report.team.name if report.team else None,
        author_name=report.author.display_name if report.author else None,
        cadence=report.cadence,
        period_start=report.period_start,
        status=status,
        recipients=list(addresses),
        detail=detail,
    )
    session.add(row)
    await session.flush()
    return row


async def deliveries(
    session: AsyncSession,
    *,
    since: datetime | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[ReportDelivery], int]:
    """The delivery log, newest first. For a super admin and nobody else."""
    query = select(ReportDelivery)
    if since is not None:
        query = query.where(ReportDelivery.created_at >= since)
    if status is not None:
        query = query.where(ReportDelivery.status == status)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await session.scalars(
            query.order_by(ReportDelivery.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


async def delivery_counts(
    session: AsyncSession, *, since: datetime | None = None
) -> dict[str, int]:
    """How many sent, failed and skipped, so a screen can say so without paging.

    Every status is present even at zero: a missing key reads as "unknown"
    where what is meant is "none", and those are different answers to "has
    anything failed this week".
    """
    query = select(ReportDelivery.status, func.count().label("n")).group_by(
        ReportDelivery.status
    )
    if since is not None:
        query = query.where(ReportDelivery.created_at >= since)
    found = {row.status: int(row.n) for row in (await session.execute(query)).all()}
    return {status.value: found.get(status.value, 0) for status in DeliveryStatus}


async def readers(session: AsyncSession, report_id: uuid.UUID) -> list[ReportRead]:
    return list(
        (
            await session.scalars(
                select(ReportRead)
                .where(ReportRead.report_id == report_id)
                .order_by(ReportRead.read_at)
            )
        ).all()
    )


# ── the view across reports ────────────────────────────────────────────

#: Worst first. ``blocked`` leads because it is a state rather than an
#: intensity — somebody is stopped — and it is the one a manager opens the
#: summary to find.
_SEVERITY_RANK = case(
    {
        IssueSeverity.BLOCKED: 0,
        IssueSeverity.HIGH: 1,
        IssueSeverity.MEDIUM: 2,
        IssueSeverity.LOW: 3,
    },
    value=ReportIssue.severity,
    else_=4,
)


async def overview(
    session: AsyncSession,
    viewer: Viewer,
    *,
    since: date,
    until: date,
    team_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """What the reports say together, over a period.

    This is the question a CEO or a manager actually asks — "how did presales
    do last month", "what is blocking us" — and it is answered over the same
    narrowed query as everything else, so an ordinary person asking it gets
    their own reports summarised and nobody else's. The rule is enforced once,
    in ``_visible``, rather than trusted to whoever writes the next caller.

    Deliberately figures and open issues rather than prose. Summarising what
    people wrote is a job for the assistant, which has the reports in front of
    it; what it cannot do for itself is count across a hundred of them.
    """
    base = _visible(select(Report), viewer).where(
        Report.status == ReportStatus.SUBMITTED,
        Report.period_end >= since,
        Report.period_start <= until,
    )
    if team_id is not None:
        base = base.where(Report.team_id == team_id)
    scope = base.subquery()

    filed = await session.scalar(select(func.count()).select_from(scope))
    people = await session.scalar(
        select(func.count(func.distinct(scope.c.author_id))).select_from(scope)
    )

    by_team = [
        {
            "team_id": str(row.team_id),
            "team": row.name,
            "reports": int(row.n),
            "people": int(row.people),
        }
        for row in (
            await session.execute(
                select(
                    scope.c.team_id,
                    Team.name,
                    func.count().label("n"),
                    func.count(func.distinct(scope.c.author_id)).label("people"),
                )
                .select_from(scope)
                .join(Team, Team.id == scope.c.team_id)
                .group_by(scope.c.team_id, Team.name)
                .order_by(func.count().desc())
            )
        ).all()
    ]

    by_author = [
        {
            "author_id": str(row.author_id),
            "author": row.display_name,
            "reports": int(row.n),
            "last_period": row.last_period.isoformat() if row.last_period else None,
        }
        for row in (
            await session.execute(
                select(
                    scope.c.author_id,
                    User.display_name,
                    func.count().label("n"),
                    func.max(scope.c.period_start).label("last_period"),
                )
                .select_from(scope)
                .join(User, User.id == scope.c.author_id)
                .group_by(scope.c.author_id, User.display_name)
                .order_by(func.count().desc())
            )
        ).all()
    ]

    # Averaged rather than summed: a total that grows because more people filed
    # says nothing about whether the work is going well.
    metric_rows = [
        {
            "key": row.key,
            "label": row.label,
            "unit": row.unit,
            "total": float(row.total or 0),
            "average": round(float(row.average or 0), 2),
            "reports": int(row.n),
        }
        for row in (
            await session.execute(
                select(
                    ReportMetric.key,
                    func.max(ReportMetric.label).label("label"),
                    func.max(ReportMetric.unit).label("unit"),
                    func.sum(func.coalesce(ReportMetric.value, ReportMetric.computed))
                    .label("total"),
                    func.avg(func.coalesce(ReportMetric.value, ReportMetric.computed))
                    .label("average"),
                    func.count().label("n"),
                )
                .select_from(scope)
                .join(ReportMetric, ReportMetric.report_id == scope.c.id)
                .group_by(ReportMetric.key)
                .order_by(ReportMetric.key)
            )
        ).all()
    ]

    open_issues = [
        {
            "id": str(row.ReportIssue.id),
            "report_id": str(row.ReportIssue.report_id),
            "title": row.ReportIssue.title,
            "detail": row.ReportIssue.detail,
            "severity": row.ReportIssue.severity,
            "waiting_on": row.ReportIssue.waiting_on,
            "team": row.name,
            "raised_by": row.display_name,
            "period_start": row.period_start.isoformat(),
        }
        for row in (
            await session.execute(
                select(ReportIssue, Team.name, User.display_name, scope.c.period_start)
                .select_from(scope)
                .join(ReportIssue, ReportIssue.report_id == scope.c.id)
                .join(Team, Team.id == scope.c.team_id)
                .join(User, User.id == scope.c.author_id)
                .where(ReportIssue.resolved.is_(False))
                # Blocked first, then worst first, then most recent: the order
                # somebody reading this needs them in, rather than the order
                # they happened to be written in.
                .order_by(_SEVERITY_RANK, scope.c.period_start.desc())
                .limit(200)
            )
        ).all()
    ]

    return {
        "since": since,
        "until": until,
        "reports": int(filed or 0),
        "people": int(people or 0),
        "by_team": by_team,
        "by_author": by_author,
        "metrics": metric_rows,
        "open_issues": open_issues,
    }
