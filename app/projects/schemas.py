"""What the projects API accepts and returns.

Two audiences and one shape. The board renders a project from these; the
assistant fills the same models in from what somebody said out loud. Keeping
them identical is what makes "move my task to 60%" and dragging a slider the
same operation rather than two that drift.

One convention worth naming: every ``*In`` model that edits an existing row
uses ``exclude_unset`` at the call site, so a field left out is untouched and a
field sent as ``null`` is cleared. Those are genuinely different intentions —
"I did not mention the due date" and "there is no due date any more" — and a
schema that could not tell them apart would make clearing a date impossible.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.models.project import (
    IssueStatus,
    MilestonePlan,
    ProjectRole,
    ProjectStatus,
    Rag,
    RagTrend,
    TaskPriority,
    TaskStatus,
)
from app.projects.progress import GRAINS

Status = Literal["planned", "active", "on_hold", "done", "cancelled"]
RagValue = Literal["green", "amber", "red", "grey"]
Trend = Literal["improving", "steady", "declining"]
Role = Literal["lead", "member", "viewer"]
TaskState = Literal["not_started", "in_progress", "blocked", "done", "dropped"]
Priority = Literal["low", "medium", "high", "critical"]
Plan = Literal["on_plan", "off_plan_no_impact", "off_plan_impact"]
IssueState = Literal["open", "in_progress", "resolved", "closed"]
WindowGrain = Literal["day", "week", "month", "quarter", "year", "custom"]

# Kept honest against the models rather than trusted to stay in step. A value
# added to an enum and forgotten here would be accepted by the service and
# refused by the schema, which is the confusing way round — the failure would
# surface as a 422 on a value the database is perfectly happy with.
assert set(Status.__args__) == set(ProjectStatus)
assert set(RagValue.__args__) == set(Rag)
assert set(Trend.__args__) == set(RagTrend)
assert set(Role.__args__) == set(ProjectRole)
assert set(TaskState.__args__) == set(TaskStatus)
assert set(Priority.__args__) == set(TaskPriority)
assert set(Plan.__args__) == set(MilestonePlan)
assert set(IssueState.__args__) == set(IssueStatus)
assert set(WindowGrain.__args__) == set(GRAINS)


# ── people ─────────────────────────────────────────────────────────────


class PersonOut(BaseModel):
    """Somebody named on a project, in the least that identifies them.

    Deliberately not the full directory record: a project page should not be a
    way to read everyone's phone number, and a card only ever needs a name.
    """

    id: uuid.UUID
    name: str
    email: str | None = None


class MemberIn(BaseModel):
    user_id: uuid.UUID
    role: Role = "member"
    responsibility: str | None = Field(default=None, max_length=200)


class MemberOut(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str | None
    role: str
    responsibility: str | None
    #: How much of the project's open work sits with them. The number that
    #: turns a member list into something a lead acts on.
    open_tasks: int = 0


# ── milestones ─────────────────────────────────────────────────────────


class MilestoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    detail: str | None = None
    owner_id: uuid.UUID | None = None
    start_on: date | None = None
    due_on: date | None = None
    is_key: bool = False


class MilestoneEditIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    detail: str | None = None
    owner_id: uuid.UUID | None = None
    start_on: date | None = None
    due_on: date | None = None
    done_on: date | None = None
    percent_complete: int | None = Field(default=None, ge=0, le=100)
    plan: Plan | None = None
    is_key: bool | None = None
    position: int | None = Field(default=None, ge=0)


class MilestoneOut(BaseModel):
    id: uuid.UUID
    position: int
    name: str
    detail: str | None
    owner: PersonOut | None
    start_on: date | None
    due_on: date | None
    done_on: date | None
    #: Where the plan first put it. A timeline draws the slip from the gap
    #: between this and ``due_on``.
    baseline_due_on: date | None
    #: Derived from its tasks when it has any, so it cannot disagree with the
    #: work underneath it.
    percent_complete: int
    plan: str
    is_key: bool
    #: "done", "due", "overdue", "upcoming" or "undated" — worked out from the
    #: dates at read time so a plan stays truthful without a nightly job.
    state: str
    #: How far it has moved from its baseline, in days. Null when it never has.
    slip_days: int | None
    task_count: int


# ── tasks ──────────────────────────────────────────────────────────────


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    detail: str | None = None
    milestone_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    status: TaskState = "not_started"
    priority: Priority = "medium"
    start_on: date | None = None
    due_on: date | None = None
    estimate_hours: Decimal | None = Field(default=None, ge=0, le=100000)


class TaskEditIn(BaseModel):
    """Change a task. Anything left out stays as it is.

    ``note`` and ``hours`` are not columns being set — they are what gets
    written into the progress log alongside the change. That is why a note with
    no other field is a valid request: "nothing moved this week, here is why"
    is one of the more useful things a report can carry.
    """

    title: str | None = Field(default=None, min_length=1, max_length=500)
    detail: str | None = None
    milestone_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    status: TaskState | None = None
    priority: Priority | None = None
    percent_complete: int | None = Field(default=None, ge=0, le=100)
    start_on: date | None = None
    due_on: date | None = None
    estimate_hours: Decimal | None = Field(default=None, ge=0, le=100000)
    blocked_reason: str | None = None
    position: int | None = Field(default=None, ge=0)

    #: Goes in the log, not on the task.
    note: str | None = Field(default=None, max_length=4000)
    #: Added to the time already spent, rather than replacing it. People report
    #: "three hours today", not "eleven hours in total".
    hours: Decimal | None = Field(default=None, ge=0, le=1000)


class TaskOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    milestone_id: uuid.UUID | None
    position: int
    title: str
    detail: str | None
    assignee: PersonOut | None
    status: str
    priority: str
    percent_complete: int
    start_on: date | None
    due_on: date | None
    done_at: datetime | None
    estimate_hours: Decimal | None
    spent_hours: Decimal | None
    blocked_reason: str | None
    #: Not done and past its date. Computed here so no frontend has to decide
    #: what late means.
    overdue: bool
    #: Whether this particular caller may move it, so a page does not have to
    #: reimplement the rules to know which rows to make editable.
    can_update: bool = False


class MyTaskOut(TaskOut):
    """A task on somebody's own list, carrying enough project to make sense.

    A person's task list spans projects, so a row that named only the task
    would be unreadable — "review the schema" means nothing without knowing
    which project wants it reviewed.
    """

    project_name: str
    project_code: str | None


# ── issues ─────────────────────────────────────────────────────────────


class IssueIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    detail: str | None = None
    priority: Priority = "medium"
    owner_id: uuid.UUID | None = None
    due_on: date | None = None
    #: Raises this into the "support needed" box on the next status report.
    needs_support: bool = False
    support_note: str | None = Field(default=None, max_length=2000)


class IssueEditIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    detail: str | None = None
    status: IssueState | None = None
    priority: Priority | None = None
    owner_id: uuid.UUID | None = None
    due_on: date | None = None
    needs_support: bool | None = None
    support_note: str | None = Field(default=None, max_length=2000)
    position: int | None = Field(default=None, ge=0)


class IssueOut(BaseModel):
    id: uuid.UUID
    position: int
    title: str
    detail: str | None
    status: str
    priority: str
    owner: PersonOut | None
    raised_on: date
    due_on: date | None
    resolved_on: date | None
    needs_support: bool
    support_note: str | None
    #: How long it has been open, in days. What turns a list of issues into a
    #: list of issues somebody has been sitting on.
    age_days: int


# ── health ─────────────────────────────────────────────────────────────


class HealthIn(BaseModel):
    """Record the lead's assessment. Every dial is optional; the review is not.

    Sending this at all stamps the project as reviewed now, even with no dial
    changed — confirming that a project is still amber is a real act and the
    board shows it as recently assessed because of it.
    """

    rag_overall: RagValue | None = None
    rag_scope: RagValue | None = None
    rag_cost: RagValue | None = None
    rag_schedule: RagValue | None = None
    rag_benefits: RagValue | None = None
    trend_overall: Trend | None = None
    trend_scope: Trend | None = None
    trend_cost: Trend | None = None
    trend_schedule: Trend | None = None
    trend_benefits: Trend | None = None
    note: str | None = Field(default=None, max_length=4000)


class DialOut(BaseModel):
    """One dial: what the lead said, and what the rows suggest.

    Both, side by side, and never merged. The stored value is a judgement and
    the suggestion is arithmetic; a screen that showed only the second would be
    overruling the person who runs the project, and one that showed only the
    first would let a dial go stale unchallenged.
    """

    key: str
    label: str
    rag: str
    trend: str
    #: What the dates or the budget would say. Null for the dials nothing can
    #: compute — scope and benefits are judgement all the way down.
    suggested: str | None = None
    suggested_reason: str | None = None
    #: True when the two disagree, which is the only case worth drawing
    #: attention to.
    differs: bool = False


class HealthOut(BaseModel):
    dials: list[DialOut]
    reviewed_at: datetime | None
    reviewed_note: str | None
    #: Nobody has looked at these for a fortnight, or ever.
    stale: bool


# ── the project itself ─────────────────────────────────────────────────


class ProjectIn(BaseModel):
    team_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=32)
    description: str | None = None
    objective: str | None = Field(default=None, max_length=4000)
    status: Status = "planned"
    lead_id: uuid.UUID | None = None
    start_on: date | None = None
    target_end_on: date | None = None
    budget_amount: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="AED", min_length=3, max_length=3)


class ProjectEditIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=32)
    description: str | None = None
    objective: str | None = Field(default=None, max_length=4000)
    status: Status | None = None
    lead_id: uuid.UUID | None = None
    start_on: date | None = None
    target_end_on: date | None = None
    actual_end_on: date | None = None
    budget_amount: Decimal | None = Field(default=None, ge=0)
    spend_amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    #: The lead's own completion figure. Null hands the number back to the
    #: tasks, which is the default and usually the right answer.
    percent_complete: int | None = Field(default=None, ge=0, le=100)


class RollupOut(BaseModel):
    tasks_total: int
    tasks_done: int
    tasks_open: int
    tasks_blocked: int
    tasks_overdue: int
    milestones_total: int
    milestones_done: int
    milestones_overdue: int
    issues_open: int
    issues_needing_support: int
    percent_complete: int


class ProjectSummaryOut(BaseModel):
    """A project in a list or on a board card."""

    id: uuid.UUID
    team_id: uuid.UUID
    team: str
    code: str | None
    name: str
    label: str
    objective: str | None
    status: str
    lead: PersonOut | None
    start_on: date | None
    target_end_on: date | None
    actual_end_on: date | None
    rag_overall: str
    trend_overall: str
    percent_complete: int
    currency: str
    budget_amount: Decimal | None
    spend_amount: Decimal | None
    archived: bool
    #: Nobody has assessed the dials recently. Shown on the card because a
    #: green project nobody has looked at is not evidence of a green project.
    health_stale: bool
    rollup: RollupOut


class ProjectOut(ProjectSummaryOut):
    description: str | None
    health: HealthOut
    members: list[MemberOut]
    milestones: list[MilestoneOut]
    tasks: list[TaskOut]
    issues: list[IssueOut]
    #: What this caller may do, so a frontend does not reimplement the rules to
    #: decide which buttons to draw.
    can_manage: bool
    can_administer: bool
    can_report: bool


class ProjectPage(BaseModel):
    projects: list[ProjectSummaryOut]
    total: int


# ── the progress log ───────────────────────────────────────────────────


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class UpdateOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    project_name: str
    task_id: uuid.UUID | None
    milestone_id: uuid.UUID | None
    issue_id: uuid.UUID | None
    author: PersonOut | None
    kind: str
    subject: str | None
    percent_before: int | None
    percent_after: int | None
    percent_delta: int | None
    status_before: str | None
    status_after: str | None
    hours: Decimal | None
    body: str | None
    created_at: datetime


class ActivityOut(BaseModel):
    """What happened over a window, which is the whole of day/week/month/year
    reporting expressed once.

    The window is echoed back rather than assumed, so a caller that asked for
    "this week" and a caller that named two dates read the response the same
    way — and so a report can print the period it actually covered rather than
    the one somebody meant.
    """

    since: date
    until: date
    grain: str
    label: str
    projects: int
    updates: list[UpdateOut]
    #: Counts by kind, so a heading can say "12 task updates, 2 milestones"
    #: without the caller tallying the list itself.
    counts: dict[str, int]


# ── the portfolio ──────────────────────────────────────────────────────


class PortfolioOut(BaseModel):
    """Every readable project rolled into one set of figures.

    Narrowed to what the caller may read, exactly as the listing is. An
    ordinary member asking gets a portfolio of their own projects rather than
    a refusal.
    """

    projects: int
    by_status: dict[str, int]
    by_rag: dict[str, int]
    tasks_open: int
    tasks_overdue: int
    issues_open: int
    issues_needing_support: int
    milestones_overdue: int
    average_percent: int
    #: How many sets of dials nobody has confirmed lately. The number a manager
    #: should read before believing any of the others.
    stale_health: int


class BoardOut(BaseModel):
    """One person's landing page: their work, and the projects it belongs to."""

    my_open_tasks: int
    my_overdue_tasks: int
    my_due_this_week: int
    tasks: list[MyTaskOut]
    projects: list[ProjectSummaryOut]
    portfolio: PortfolioOut
