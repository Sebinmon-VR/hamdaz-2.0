"""What the reports API accepts and returns.

Two audiences and one shape. The page renders a report from these; the
assistant fills the same models in from what somebody said out loud. Keeping
them identical is what makes "create my daily report" and clicking New the same
operation rather than two that drift.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.report import BriefFollowup, BriefMode
from app.reports.catalogue import COMPLETIONS

Cadence = Literal["daily", "weekly", "monthly", "quarterly", "yearly", "ad_hoc"]
Scope = Literal["team", "project", "portfolio"]
Completion = Literal["not_started", "in_progress", "blocked", "done", "dropped"]
Severity = Literal["low", "medium", "high", "blocked"]
BriefModeName = Literal["on_submit", "on_first_open", "on_request"]
BriefFollowupName = Literal["off", "refresh", "chat"]

# Kept honest against the catalogue rather than trusted to stay in step: a
# completion added there and forgotten here would be accepted by the service
# and refused by the schema, which is the confusing way round.
assert set(COMPLETIONS) == set(Completion.__args__)
assert set(BriefMode) == set(BriefModeName.__args__)
assert set(BriefFollowup) == set(BriefFollowupName.__args__)


# ── projects on a report ───────────────────────────────────────────────


class ProjectChoiceOut(BaseModel):
    """A project somebody may file a status report on, for the picker."""

    id: uuid.UUID
    name: str
    code: str | None
    label: str
    status: str
    rag_overall: str
    percent_complete: int
    #: Whether they already filed on this project for this period. Shown so the
    #: picker can grey it out rather than letting somebody choose it and then
    #: be refused.
    already_reported: bool = False


class MilestoneLineOut(BaseModel):
    """One milestone on a filed report — a bar on the timeline.

    Carries the original date beside the current one, because the gap between
    them is the most useful thing on a project timeline and it disappears the
    moment only one is kept.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    milestone_id: uuid.UUID | None
    name: str
    owner_name: str | None
    start_on: date | None
    due_on: date | None
    done_on: date | None
    baseline_due_on: date | None
    #: The "PoC" column of a project status report.
    percent_complete: int
    plan: str | None
    #: How it stood when the report was filed. Stored rather than recomputed,
    #: so a report written in March still describes March in June.
    state: str | None
    is_key: bool
    note: str | None


class ProjectLineOut(BaseModel):
    """One project as it stood when the report was filed.

    Every figure here is a snapshot. The project it came from has almost
    certainly moved since, and following ``project_id`` is how somebody reaches
    the live version — the numbers below are deliberately frozen, because a
    report that changed after it was filed would not be a report.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    #: Null once the project itself has been deleted. Everything else on this
    #: row still reads, which is the whole reason it is a copy.
    project_id: uuid.UUID | None
    name: str
    code: str | None
    lead_name: str | None
    status: str | None
    start_on: date | None
    target_end_on: date | None

    rag_overall: str | None
    rag_scope: str | None
    rag_cost: str | None
    rag_schedule: str | None
    rag_benefits: str | None
    trend_overall: str | None
    trend_scope: str | None
    trend_cost: str | None
    trend_schedule: str | None
    trend_benefits: str | None

    percent_complete: int
    tasks_total: int
    tasks_done: int
    tasks_open: int
    tasks_blocked: int
    tasks_overdue: int
    milestones_total: int
    milestones_done: int
    milestones_overdue: int
    issues_open: int
    #: Movement inside the window this report covers, rather than the running
    #: total. The figures that make a weekly report about the week.
    updates_in_period: int
    tasks_completed_in_period: int

    budget_amount: Decimal | None
    spend_amount: Decimal | None
    currency: str | None

    #: The author's own words — the "key activities" and "management action
    #: required" of a project status report. The only part of this row a person
    #: types, and the only part they may edit.
    activities: str | None
    action_required: str | None
    note: str | None

    milestones: list[MilestoneLineOut]


class ProjectNoteIn(BaseModel):
    """The narrative on one project line. Figures are not editable.

    Only the prose: the numbers beside it are a snapshot taken when the draft
    was opened, and letting somebody type over them would turn a record of what
    the project said into a record of what they wished it had said. A project
    whose figures are wrong is fixed in the project, and the draft re-opened.
    """

    activities: str | None = Field(default=None, max_length=8000)
    action_required: str | None = Field(default=None, max_length=8000)
    note: str | None = Field(default=None, max_length=8000)


# ── the sections, as the frontend needs them ───────────────────────────


class SectionOut(BaseModel):
    key: str
    name: str
    description: str
    #: "prose", "rows" or "figures". A frontend renders from this alone.
    kind: str


class ReportFieldOut(BaseModel):
    """One extra question this team is asked, inside one of the six sections."""

    key: str
    label: str
    type: str
    section: str
    required: bool = False
    help: str | None = None
    options: list[str] | None = None


class ReportFormOut(BaseModel):
    """Everything needed to draw the form for one team and cadence.

    Served before a report exists, so the page and the assistant both know what
    is going to be asked before anybody starts answering.
    """

    team_id: uuid.UUID
    team: str
    cadence: Cadence
    template_id: uuid.UUID
    template_name: str
    template_version: int
    #: "team", "project" or "portfolio" — worked out from the sections this
    #: team's template declares. A frontend reads this to know whether to ask
    #: which project the report is about before anything else.
    scope: Scope
    #: Only for a project-scoped form: the projects this person may file a
    #: report on for this team, so the page can offer a choice rather than
    #: making somebody paste an id.
    projects: list[ProjectChoiceOut] = Field(default_factory=list)
    sections: list[SectionOut]
    fields: list[ReportFieldOut]
    completions: list[str]
    #: The period a report started now would cover.
    period_start: date
    period_end: date
    period_label: str


# ── writing one ────────────────────────────────────────────────────────


class TaskLineIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    completion: Completion = "in_progress"
    #: "proposals" for a pulled row, "manual" for a typed one. A caller may
    #: leave it: what matters downstream is the external id, not the label.
    source: Literal["proposals", "manual"] = "manual"
    external_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=80)
    percent_complete: int | None = Field(default=None, ge=0, le=100)
    priority: str | None = Field(default=None, max_length=40)
    end_user: str | None = Field(default=None, max_length=200)
    quote_no: str | None = Field(default=None, max_length=80)
    deadline: date | None = None
    link: str | None = None
    attachments_url: str | None = None
    has_attachments: bool = False
    note: str | None = None


class IssueIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    detail: str | None = None
    severity: Severity = "medium"
    waiting_on: str | None = Field(default=None, max_length=200)
    resolved: bool = False


class ReportStartIn(BaseModel):
    """Open a draft. Everything but the team and the cadence is optional."""

    team_id: uuid.UUID
    cadence: Cadence = "daily"
    #: Any day inside the period being reported on. Defaults to today, so
    #: "my daily report" needs no date at all.
    on: date | None = None
    #: Ad-hoc only: the period is whatever the author says, because nothing
    #: else can work it out.
    period_start: date | None = None
    period_end: date | None = None
    #: Pull the caller's own Proposals tasks in as rows. On by default: the
    #: work is already recorded somewhere and retyping it is how a reporting
    #: tool earns its reputation.
    prefill_tasks: bool = True
    #: Include tasks already finished. Off by default for a daily, where what
    #: matters is what is live.
    include_closed: bool = False

    #: Required when the team's template for this cadence is a project status
    #: report, and refused otherwise. A portfolio report covers every project
    #: the author may see on the team and so names none.
    project_id: uuid.UUID | None = None
    #: Pull the project's milestones onto the report as a timeline. On by
    #: default — a status report without its milestones is a status report
    #: missing the thing people open it for.
    prefill_milestones: bool = True


def _as_mapping(value: Any) -> Any:
    """Accept ``{"quotes_sent": 7}`` or ``[{"key": ..., "value": ...}]``.

    The page sends a mapping, which is the natural shape. The assistant cannot:
    OpenAI's strict tool schemas have no way to express "an object whose keys I
    do not know in advance" — an open object is rejected outright — and the keys
    here are whatever this team's template happens to ask for. So it sends
    pairs, and they are normalised to the same thing on the way in rather than
    the two callers being given two different endpoints.
    """
    if isinstance(value, list):
        out: dict[str, Any] = {}
        for entry in value:
            if isinstance(entry, dict) and "key" in entry:
                out[str(entry["key"])] = entry.get("value")
        return out
    return value


class ReportEditIn(BaseModel):
    """Change a draft. Only the fields present change.

    A list sent at all replaces that section entirely — sending ``tasks`` means
    "these are the tasks", not "add these". Partial row edits would need stable
    row ids on the client and are the sort of thing that goes wrong silently;
    replacing is unambiguous and the payloads are small.
    """

    overview: str | None = None
    remarks: str | None = None
    summary: str | None = None
    #: The team's own questions, keyed by template field. Merged into what is
    #: already there rather than replacing it, unlike the lists below: these are
    #: individual questions, and filling a long form over two saves should not
    #: wipe the first save. Send a key with nothing in it to clear that answer.
    answers: dict[str, Any] | None = None
    tasks: list[TaskLineIn] | None = None
    issues: list[IssueIn] | None = None
    #: Metric key to value. A computed metric given a value here is overridden
    #: and the computed figure is kept beside it; an unknown key becomes a
    #: metric of its own, which is how a template's typed figures are stored.
    metrics: dict[str, Decimal | None] | None = None
    #: The narrative on each project line, keyed by that line's id. Merged, not
    #: replaced — a portfolio report is written a project at a time, and saving
    #: one row's notes must not clear the five above it.
    project_notes: dict[uuid.UUID, ProjectNoteIn] | None = None

    _pairs = field_validator("answers", "metrics", mode="before")(
        staticmethod(_as_mapping)
    )


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


# ── reading one ────────────────────────────────────────────────────────


class TaskLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    source: str
    external_id: str | None
    title: str
    status: str | None
    completion: str
    percent_complete: int | None
    priority: str | None
    end_user: str | None
    quote_no: str | None
    deadline: date | None
    #: Into SharePoint, where the work actually happens. Carries no token —
    #: opening it uses the reader's own access.
    link: str | None
    attachments_url: str | None
    has_attachments: bool
    note: str | None


class IssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    title: str
    detail: str | None
    severity: str
    waiting_on: str | None
    resolved: bool


class MetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    label: str
    unit: str | None
    #: What the task rows said.
    computed: Decimal | None
    #: What the author put, if they put anything.
    value: Decimal | None
    target: Decimal | None
    #: The figure that counts.
    effective: Decimal | None
    #: Whether somebody moved it off the computed figure. Worth showing: a
    #: corrected number and an agreeing one are different kinds of evidence.
    edited: bool


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID
    author_name: str
    body: str
    created_at: datetime


class ReportSummaryOut(BaseModel):
    """A report in a list: enough to choose one, not enough to leak one."""

    id: uuid.UUID
    team_id: uuid.UUID
    team: str
    author_id: uuid.UUID
    author_name: str
    cadence: str
    period_start: date
    period_end: date
    period_label: str
    #: "team", "project" or "portfolio".
    scope: str
    #: The project a project-scoped report is about. Null for the other two,
    #: and also null once that project has been deleted — the report's own
    #: project line keeps the name either way.
    project_id: uuid.UUID | None
    project_name: str | None
    status: str
    submitted_at: datetime | None
    task_count: int
    open_issue_count: int
    #: Only for a reader who is not the author, so a manager can see at a
    #: glance what they have not got to yet.
    read_by_me: bool = False


class ReportOut(ReportSummaryOut):
    template_id: uuid.UUID
    template_name: str
    template_version: int
    overview: str | None
    remarks: str | None
    summary: str | None
    answers: dict[str, Any]
    sections: list[SectionOut]
    fields: list[ReportFieldOut]
    tasks: list[TaskLineOut]
    issues: list[IssueOut]
    metrics: list[MetricOut]
    #: One row for a project status report, one per project for a portfolio
    #: report, and empty for a team report. Same shape either way — the two
    #: layouts are the same figures arranged differently.
    project_lines: list[ProjectLineOut]
    comments: list[CommentOut]
    #: What this particular caller may do with it, so a frontend does not have
    #: to reimplement the rules to decide which buttons to draw.
    can_edit: bool
    can_submit: bool
    can_comment: bool
    can_delete: bool


class ReportPage(BaseModel):
    reports: list[ReportSummaryOut]
    total: int


# ── scheduling ─────────────────────────────────────────────────────────


class ScheduleIn(BaseModel):
    team_id: uuid.UUID
    cadence: Cadence
    template_id: uuid.UUID
    enabled: bool = True
    due_hour: int = Field(default=18, ge=0, le=23)
    #: 0 is Monday. Only meaningful for a weekly.
    due_weekday: int | None = Field(default=None, ge=0, le=6)
    note: str | None = Field(default=None, max_length=2000)
    #: Null follows the global setting; false silences this team's reports of
    #: this cadence only. Three states rather than two, so switching the global
    #: setting on later reaches a team that never expressed a preference and
    #: does not reach one that said no.
    notify: bool | None = None
    #: Copied on this team's reports only. Added to whatever the global
    #: settings already produce — this narrows nothing.
    extra_recipients: list[str] = Field(default_factory=list, max_length=50)


class ScheduleOut(BaseModel):
    id: uuid.UUID
    team_id: uuid.UUID
    team: str
    cadence: str
    template_id: uuid.UUID
    template_name: str
    enabled: bool
    due_hour: int
    due_weekday: int | None
    note: str | None
    notify: bool | None
    extra_recipients: list[str]


# ── the super admin's settings ─────────────────────────────────────────


class BriefOut(BaseModel):
    """The short version of one report, and what the reader may do with it.

    ``state`` is what the box renders from, and it has five answers rather than
    "is there a brief": *disabled* (the administrator has not turned this on),
    *not_applicable* (a draft — briefs are for filed reports), *absent* (nobody
    has asked for one yet), *failed* (we tried and could not), *stale* (the
    report changed after it was written) and *ready*. Collapsing those into a
    null brief would leave the page unable to say anything useful about why.
    """

    report_id: uuid.UUID
    state: Literal["disabled", "not_applicable", "absent", "failed", "stale", "ready"]
    headline: str | None = None
    body: str | None = None
    generated_at: datetime | None = None
    model: str | None = None
    #: How many times it has been written. 2 or more means somebody asked again.
    revision: int = 0
    #: Why the last attempt failed, when one did.
    error: str | None = None
    #: Whether this reader may ask for it to be written again, and whether they
    #: may ask it questions. Both follow the administrator's ``brief_followup``.
    may_refresh: bool = False
    may_chat: bool = False


class BriefChatOut(BaseModel):
    """Where to carry on the conversation about this report.

    The box on the page is the assistant, not a copy of it: this hands back a
    real conversation id, and everything after the first message goes through
    ``/assistant/conversations/{id}/messages`` like any other chat. One less
    streaming endpoint to keep in step, and the run shows up in the assistant's
    own cost and audit screens rather than in a blind spot.
    """

    conversation_id: uuid.UUID
    #: True when this call created it. False means the reader is being handed
    #: back the questions they already asked about this report.
    created: bool
    brief: BriefOut


class ReportSettingsOut(BaseModel):
    """Who filed reports go to, and how much of one the message carries.

    None of this widens who may *read* a report. An address in
    ``extra_recipients`` gets a summary; the link in it refuses them like any
    other person who may not read it.
    """

    model_config = ConfigDict(from_attributes=True)

    notify_on_submit: bool
    notify_team_oversight: bool
    notify_company_wide: bool
    company_roles: list[str]
    extra_recipients: list[str]
    copy_author: bool
    notify_cadences: list[str]
    max_tasks_in_email: int
    include_task_list: bool
    include_issue_list: bool
    log_retention_days: int

    #: The AI summariser. Off by default: it is the only part of the module
    #: that spends money per report.
    brief_enabled: bool
    brief_mode: str
    brief_followup: str
    #: Null follows whatever model the assistant itself is set to.
    brief_model_key: str | None
    brief_max_words: int

    updated_by_id: uuid.UUID | None
    updated_at: datetime


class ReportSettingsIn(BaseModel):
    """Only the fields given change."""

    notify_on_submit: bool | None = None
    notify_team_oversight: bool | None = None
    notify_company_wide: bool | None = None
    #: Global role keys. Every one must exist: a key that does not would
    #: silently mail nobody, which is the failure nobody notices until
    #: somebody asks why they stopped getting reports.
    company_roles: list[str] | None = Field(default=None, max_length=20)
    #: Anything without an "@" is dropped rather than failing the whole save.
    extra_recipients: list[str] | None = Field(default=None, max_length=50)
    copy_author: bool | None = None
    notify_cadences: list[str] | None = Field(default=None, max_length=4)
    max_tasks_in_email: int | None = Field(default=None, ge=0, le=100)
    include_task_list: bool | None = None
    include_issue_list: bool | None = None
    log_retention_days: int | None = Field(default=None, ge=1, le=3650)

    #: The AI summariser.
    brief_enabled: bool | None = None
    #: ``on_submit`` writes the brief as the report is filed, ``on_first_open``
    #: when the first manager opens it, ``on_request`` only when asked.
    brief_mode: BriefModeName | None = None
    #: What a reader may do with one: ``off`` read it, ``refresh`` ask for
    #: another, ``chat`` ask it questions.
    brief_followup: BriefFollowupName | None = None
    #: An assistant model key, or empty to follow the assistant's own setting.
    brief_model_key: str | None = Field(default=None, max_length=64)
    brief_max_words: int | None = Field(default=None, ge=40, le=600)


class DeliveryOut(BaseModel):
    """One attempt to mail one report.

    The names are copied off the report rather than joined, so a record still
    reads after its report is deleted — which is exactly the record somebody is
    trying to look up.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    report_id: uuid.UUID | None
    team_name: str | None
    author_name: str | None
    cadence: str | None
    period_start: date | None
    #: "sent", "failed" or "skipped".
    status: str
    recipients: list[str]
    #: Why nothing was sent, or why it failed. Null on a clean send.
    detail: str | None
    created_at: datetime


class DeliveryPage(BaseModel):
    deliveries: list[DeliveryOut]
    total: int
    #: How many of each status over the window asked for, so the screen can say
    #: "3 failed this week" without counting rows itself.
    counts: dict[str, int]


class TemplateChoiceOut(BaseModel):
    """A report template an administrator may point a team at."""

    id: uuid.UUID
    key: str
    name: str
    description: str | None
    version: int
    field_count: int


# ── the view across reports ────────────────────────────────────────────


class TeamRollupOut(BaseModel):
    team_id: str
    team: str
    reports: int
    people: int


class AuthorRollupOut(BaseModel):
    author_id: str
    author: str
    reports: int
    last_period: str | None


class MetricRollupOut(BaseModel):
    key: str
    label: str
    unit: str | None
    total: float
    #: Averaged as well as totalled: a total that grows because more people
    #: filed says nothing about whether the work is going well.
    average: float
    reports: int


class OpenIssueOut(BaseModel):
    id: str
    report_id: str
    title: str
    detail: str | None
    severity: str
    waiting_on: str | None
    team: str
    raised_by: str
    period_start: str


class OverviewOut(BaseModel):
    """What the reports say together — the question a CEO actually asks.

    Narrowed to what the caller may read, exactly as the listing is. An
    ordinary person asking for this gets their own reports summarised and
    nobody else's, rather than a refusal.
    """

    since: date
    until: date
    reports: int
    people: int
    by_team: list[TeamRollupOut]
    by_author: list[AuthorRollupOut]
    metrics: list[MetricRollupOut]
    open_issues: list[OpenIssueOut]
