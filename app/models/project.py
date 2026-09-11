"""Projects, the work inside them, and how far along any of it is.

A project is a named piece of work a team owns, with milestones to hit, tasks
people are assigned, issues in the way, and a running account of what moved and
when. It is the thing the AI team asked for and the thing a project status
report is written about.

**Assignment is the visibility model.** Membership of a project is what makes
it yours to see; being assigned a task inside it is what makes that task yours
to update. Everything else follows from those two rows, which is why they are
tables rather than a list of ids on the project — "what is Ravi working on
across every project" is a question somebody asks in week two, and a JSONB
array cannot answer it.

**Health is judged, not computed.** The five dials on a status report — overall,
scope, cost, schedule, benefits — are a lead's assessment and are stored as
such. Percentages and dates can say a project is behind; only a person can say
whether being behind matters. What the module does compute is a *suggestion*
(see ``app.projects.progress``), shown next to the stored value so a dial that
has gone stale is visible rather than quietly wrong.

**Progress is a log, not a field.** ``ProjectUpdate`` records each movement —
who, when, from what to what, and why. A single ``percent_complete`` column
answers "where is this now" and nothing else; the questions a weekly report
exists to answer are all about a window of time, and a window needs history.
That is what makes day-wise, week-wise, monthly and yearly reporting the same
query with different bounds rather than four features.

Nothing here reaches SharePoint. Proposals are bids the presales team works;
projects are internal work with a plan, and conflating the two would mean
every project inheriting a schema built for tenders.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.team import Team
from app.models.user import User


class ProjectStatus(StrEnum):
    """Where a project is in its life. Not the same thing as its health.

    A project can be ``ACTIVE`` and red, or ``ON_HOLD`` and green — one says
    whether work is happening, the other says whether it is going well. Merging
    them into a single "status" is how a portfolio view stops being able to
    answer either question.
    """

    PLANNED = "planned"
    ACTIVE = "active"
    #: Deliberately stopped. Keeps its people, its plan and its history, and
    #: drops out of "what is live" without anyone having to delete anything.
    ON_HOLD = "on_hold"
    DONE = "done"
    #: Stopped and not coming back. Kept apart from ``DONE`` because a
    #: completion rate that counts cancellations as deliveries is a number
    #: nobody should be shown.
    CANCELLED = "cancelled"


#: The statuses that mean work is expected to be happening. What "my live
#: projects" means, and what the portfolio dashboard counts by default.
OPEN_PROJECT_STATUSES: frozenset[str] = frozenset(
    {ProjectStatus.PLANNED, ProjectStatus.ACTIVE, ProjectStatus.ON_HOLD}
)

#: Projects that have stopped, whether they landed or not.
CLOSED_PROJECT_STATUSES: frozenset[str] = frozenset(
    {ProjectStatus.DONE, ProjectStatus.CANCELLED}
)


class Rag(StrEnum):
    """One health dial, in the language every status report on earth uses.

    ``GREY`` is not a fourth severity — it means nobody has assessed this yet,
    and it is the default. A project that has never been judged showing green
    is the single most misleading thing a portfolio page can do, because green
    is indistinguishable from "fine" at a glance and the whole point of the
    board is glancing at it.
    """

    GREEN = "green"
    AMBER = "amber"
    RED = "red"
    GREY = "grey"


class RagTrend(StrEnum):
    """Which way a dial is moving — the chevron next to it on a status report.

    Worth storing separately from the colour because they say different things:
    amber-improving is a project being recovered and amber-declining is a
    project about to go red, and a manager reading a portfolio needs to spend
    their attention on the second one.
    """

    IMPROVING = "improving"
    STEADY = "steady"
    DECLINING = "declining"


class ProjectRole(StrEnum):
    """What somebody is on one project. Distinct from their role in the team.

    A team lead is not automatically a project lead, and a project lead need
    not be senior — the person running the migration may be the person who
    knows the migration. Keeping this per project is what lets both be true.
    """

    LEAD = "lead"
    MEMBER = "member"
    #: Reads it, holds none of it. For a stakeholder who should see the board
    #: without appearing in workload counts or the assignment pool.
    VIEWER = "viewer"


class TaskStatus(StrEnum):
    """How far along one task is.

    **The values are deliberately identical to ``reports.catalogue.COMPLETIONS``.**
    A project task that lands on a status report should carry its own state
    across unchanged; a mapping table between two nearly-equal vocabularies is
    a thing that goes subtly wrong at the one value nobody tested.
    """

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    DROPPED = "dropped"


#: Tasks that still need somebody. Used for workload, for "what is open on this
#: project", and for the counts on a report.
OPEN_TASK_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.NOT_STARTED, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}
)


class TaskPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    #: Someone should be working on this now. Rare by design — a priority
    #: everything can have is a priority nothing has.
    CRITICAL = "critical"


class MilestonePlan(StrEnum):
    """Whether a milestone is still where the plan put it, and what it costs.

    The three markers on the reference status report. The distinction that
    matters is the last two: a milestone that has slipped without consequence
    is information, and one that has slipped and pushed something else is a
    decision somebody has to make. A single "late" flag cannot tell a manager
    which of those they are looking at.
    """

    ON_PLAN = "on_plan"
    #: Moved, nothing downstream cares.
    OFF_PLAN_NO_IMPACT = "off_plan_no_impact"
    #: Moved, and something else moves because of it.
    OFF_PLAN_IMPACT = "off_plan_impact"


class IssueStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    #: Closed without being fixed — overtaken, withdrawn, or decided against.
    #: Separate from ``RESOLVED`` so "how many did we actually solve" stays a
    #: question with an answer.
    CLOSED = "closed"


OPEN_ISSUE_STATUSES: frozenset[str] = frozenset({IssueStatus.OPEN, IssueStatus.IN_PROGRESS})


class UpdateKind(StrEnum):
    """What sort of movement an entry in the progress log records."""

    #: A task moved — percentage, status, or both.
    TASK = "task"
    #: The project's own health or percentage was reassessed.
    HEALTH = "health"
    #: A milestone was hit, moved, or reassessed.
    MILESTONE = "milestone"
    #: An issue was raised or closed.
    ISSUE = "issue"
    #: Somebody wrote something down. No numbers moved.
    NOTE = "note"


class Project(Base, UUIDPrimaryKey, Timestamped):
    """One piece of work a team owns.

    The five health columns and their trends are what a status report's dials
    are drawn from, and they are stored on the project rather than typed into
    each report on purpose: the report is a snapshot of the project's health at
    a moment, not a separate opinion about it. A lead who changes a dial changes
    it once, and every view — the board, the next report, the portfolio roll-up
    — agrees without anybody reconciling anything.
    """

    __tablename__ = "projects"
    __table_args__ = (
        # A code is how people refer to a project in a meeting, so it has to be
        # stable and unique — but only within its team. Two teams both wanting
        # "PH-1" is not a conflict worth refusing.
        UniqueConstraint("team_id", "code", name="uq_project_team_code"),
        Index("ix_projects_team_status", "team_id", "status"),
        Index("ix_projects_lead", "lead_id"),
        CheckConstraint(
            "percent_complete IS NULL OR (percent_complete >= 0 AND percent_complete <= 100)",
            name="ck_project_percent_range",
        ),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Short handle, e.g. "AI-01". Optional: a team that does not use codes
    #: should not be made to invent them.
    code: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: One or two lines a manager reads instead of the whole project. Written
    #: once and edited rarely, unlike the report narrative which is per period.
    objective: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(16), default=ProjectStatus.PLANNED,
        server_default=text("'planned'"), nullable=False,
    )
    #: Who runs it. Nullable because a project can be created before it is
    #: handed to anybody, and a required field there just gets filled with
    #: whoever created it.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    start_on: Mapped[date | None] = mapped_column(Date)
    #: What was promised. Kept even after it is missed — a target that moves
    #: silently to match reality is not a target.
    target_end_on: Mapped[date | None] = mapped_column(Date)
    actual_end_on: Mapped[date | None] = mapped_column(Date)

    # ── the five dials ─────────────────────────────────────────────────
    #: The lead's overall judgement. Not derived from the other four: a project
    #: can be green on every dimension and red overall because of something
    #: none of them covers, and the person running it is entitled to say so.
    rag_overall: Mapped[str] = mapped_column(
        String(8), default=Rag.GREY, server_default=text("'grey'"), nullable=False, index=True
    )
    rag_scope: Mapped[str] = mapped_column(
        String(8), default=Rag.GREY, server_default=text("'grey'"), nullable=False
    )
    rag_cost: Mapped[str] = mapped_column(
        String(8), default=Rag.GREY, server_default=text("'grey'"), nullable=False
    )
    rag_schedule: Mapped[str] = mapped_column(
        String(8), default=Rag.GREY, server_default=text("'grey'"), nullable=False
    )
    rag_benefits: Mapped[str] = mapped_column(
        String(8), default=Rag.GREY, server_default=text("'grey'"), nullable=False
    )

    trend_overall: Mapped[str] = mapped_column(
        String(12), default=RagTrend.STEADY, server_default=text("'steady'"), nullable=False
    )
    trend_scope: Mapped[str] = mapped_column(
        String(12), default=RagTrend.STEADY, server_default=text("'steady'"), nullable=False
    )
    trend_cost: Mapped[str] = mapped_column(
        String(12), default=RagTrend.STEADY, server_default=text("'steady'"), nullable=False
    )
    trend_schedule: Mapped[str] = mapped_column(
        String(12), default=RagTrend.STEADY, server_default=text("'steady'"), nullable=False
    )
    trend_benefits: Mapped[str] = mapped_column(
        String(12), default=RagTrend.STEADY, server_default=text("'steady'"), nullable=False
    )

    #: When somebody last actually looked at the dials above. A red project
    #: nobody has touched for a month and a red project reviewed this morning
    #: are different situations, and only this column tells them apart.
    health_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_note: Mapped[str | None] = mapped_column(Text)

    # ── progress and money ─────────────────────────────────────────────
    #: The lead's own figure, when they set one. Null means "use what the tasks
    #: say" — see ``app.projects.progress``. Two columns would let the stored
    #: and derived numbers disagree with no way to tell which was meant;
    #: nullable-with-fallback makes the override explicit.
    percent_complete: Mapped[int | None] = mapped_column(Integer)

    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    spend_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    #: What the money is in. Held per project rather than assumed, because a
    #: figure without a currency is the kind of number that gets added up.
    currency: Mapped[str] = mapped_column(
        String(3), default="AED", server_default=text("'AED'"), nullable=False
    )

    #: Room for whatever a team tracks that nobody else does. Deliberately not
    #: where anything queryable goes — the same rule the reports module follows.
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    #: Archived projects keep everything and drop out of normal listings, the
    #: same arrangement teams use.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    team: Mapped[Team] = relationship(lazy="joined")
    lead: Mapped[User | None] = relationship(foreign_keys=[lead_id], lazy="joined")

    #: Loaded with the project. Both are bounded — a project with two hundred
    #: milestones has a different problem — and both are wanted every time a
    #: project is shown at all.
    members: Mapped[list[ProjectMember]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin",
    )
    milestones: Mapped[list[ProjectMilestone]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectMilestone.position",
        lazy="selectin",
    )

    #: **Not** eagerly loaded, unlike the two above. A project accumulates
    #: hundreds of tasks and thousands of updates, and a listing of thirty
    #: projects must not drag them along. Every path that needs these loads them
    #: explicitly with ``selectinload`` — see ``app.projects.service.get_full``;
    #: listings use the SQL roll-ups in that module instead, which is why no
    #: caller ever has a reason to touch these attributes on a bare listing.
    tasks: Mapped[list[ProjectTask]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectTask.position",
    )
    issues: Mapped[list[ProjectIssue]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectIssue.position",
    )
    updates: Mapped[list[ProjectUpdate]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectUpdate.created_at.desc()",
    )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_PROJECT_STATUSES

    @property
    def label(self) -> str:
        """How the project reads in a list or an email subject."""
        return f"{self.code} — {self.name}" if self.code else self.name

    def __repr__(self) -> str:
        return f"<Project {self.code or self.name} {self.status} {self.rag_overall}>"


class ProjectMember(Base, UUIDPrimaryKey, Timestamped):
    """Somebody on a project, and what they are on it.

    One row per person, unlike ``TeamMembership`` which allows several roles at
    once. A project has one relationship to each person by design: being both
    lead and member of the same project is not a distinction anything acts on,
    and allowing it would make "who leads this" a query with more than one
    answer.
    """

    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_member"),
        # "Every project this person is on" is the query behind their own
        # dashboard, so it gets an index of its own.
        Index("ix_project_members_user", "user_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(
        String(16), default=ProjectRole.MEMBER,
        server_default=text("'member'"), nullable=False,
    )
    #: What they do here — "data pipeline", "evaluation". Free text because the
    #: list of things people do on projects is not one anybody can enumerate.
    responsibility: Mapped[str | None] = mapped_column(String(200))

    added_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    project: Mapped[Project] = relationship(back_populates="members")
    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")

    def __repr__(self) -> str:
        return f"<ProjectMember {self.user_id} {self.role}>"


class ProjectMilestone(Base, UUIDPrimaryKey, Timestamped):
    """A dated point the project is meant to reach.

    This is what the timeline on a status report is drawn from: a name, a bar
    from ``start_on`` to ``due_on``, a marker saying whether it is still where
    the plan put it, and an owner. Nothing here is computed at write time —
    a milestone that has slipped is worked out from its dates when it is read,
    so a plan does not need a nightly job to stay truthful.
    """

    __tablename__ = "project_milestones"
    __table_args__ = (
        Index("ix_project_milestones_project", "project_id", "position"),
        Index("ix_project_milestones_due", "due_on"),
        CheckConstraint(
            "percent_complete >= 0 AND percent_complete <= 100",
            name="ck_milestone_percent_range",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    start_on: Mapped[date | None] = mapped_column(Date)
    due_on: Mapped[date | None] = mapped_column(Date)
    done_on: Mapped[date | None] = mapped_column(Date)
    #: What the date was before anybody moved it. Set the first time ``due_on``
    #: changes and never again, so "how far has this slipped" stays answerable
    #: after the third reschedule — which is the point at which somebody starts
    #: asking.
    baseline_due_on: Mapped[date | None] = mapped_column(Date)

    percent_complete: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    plan: Mapped[str] = mapped_column(
        String(24), default=MilestonePlan.ON_PLAN,
        server_default=text("'on_plan'"), nullable=False,
    )
    #: The diamonds on the reference report. A timeline where everything is a
    #: key milestone is a timeline nobody can read at a glance.
    is_key: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="milestones")
    owner: Mapped[User | None] = relationship(foreign_keys=[owner_id], lazy="joined")

    @property
    def is_done(self) -> bool:
        return self.done_on is not None or self.percent_complete >= 100

    def __repr__(self) -> str:
        return f"<ProjectMilestone {self.name!r} due={self.due_on}>"


class ProjectTask(Base, UUIDPrimaryKey, Timestamped):
    """One piece of work inside a project, held by one person.

    ``assignee_id`` is the whole of the per-person view: "my tasks" is this
    column, and a task with nobody on it is deliberately possible — a plan is
    usually written before it is shared out, and forcing an assignee at
    creation means every unallocated task is quietly parked on whoever typed it.
    """

    __tablename__ = "project_tasks"
    __table_args__ = (
        Index("ix_project_tasks_project", "project_id", "position"),
        # "My open work, soonest first" — the query behind every person's own
        # page, and the one that has to stay fast as the table grows.
        Index("ix_project_tasks_assignee_status", "assignee_id", "status", "due_on"),
        Index("ix_project_tasks_milestone", "milestone_id"),
        CheckConstraint(
            "percent_complete >= 0 AND percent_complete <= 100",
            name="ck_task_percent_range",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    #: Which milestone this counts towards, if any. ``SET NULL`` rather than
    #: cascade: deleting a milestone is a re-plan, and it must not take the work
    #: underneath it with it.
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_milestones.id", ondelete="SET NULL")
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)

    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=TaskStatus.NOT_STARTED,
        server_default=text("'not_started'"), nullable=False,
    )
    priority: Mapped[str] = mapped_column(
        String(12), default=TaskPriority.MEDIUM,
        server_default=text("'medium'"), nullable=False,
    )
    percent_complete: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    start_on: Mapped[date | None] = mapped_column(Date)
    due_on: Mapped[date | None] = mapped_column(Date)
    #: Stamped when the status first becomes ``done``, and cleared if it is
    #: reopened. A report over last week needs "finished in this window",
    #: which the status alone cannot answer.
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    estimate_hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    spent_hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))

    #: Why it is blocked, in the assignee's words. Kept on the task rather than
    #: forced into a project issue: most blockers last an afternoon, and making
    #: somebody raise a formal issue to say "waiting on the API key" means they
    #: will instead say nothing.
    blocked_reason: Mapped[str | None] = mapped_column(Text)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    project: Mapped[Project] = relationship(back_populates="tasks")
    milestone: Mapped[ProjectMilestone | None] = relationship(lazy="joined")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id], lazy="joined")

    @property
    def is_done(self) -> bool:
        return self.status == TaskStatus.DONE

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_TASK_STATUSES

    def __repr__(self) -> str:
        return f"<ProjectTask {self.title[:40]!r} {self.status}>"


class ProjectIssue(Base, UUIDPrimaryKey, Timestamped):
    """Something in the way, and who is dealing with it.

    The "key issues" table on a status report. Separate from a blocked task
    because the two are read by different people for different reasons: a
    blocked task tells the lead what to unstick this afternoon, an issue tells
    a manager what to decide this week. ``needs_support`` is the flag that
    escalates one to the other — it is what fills the "support needed" box on
    the report, and it is a column rather than a severity level because
    "serious" and "needs somebody above me" are genuinely different claims.
    """

    __tablename__ = "project_issues"
    __table_args__ = (
        Index("ix_project_issues_project", "project_id", "position"),
        Index("ix_project_issues_status", "status"),
        Index("ix_project_issues_raised", "raised_on"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), default=IssueStatus.OPEN,
        server_default=text("'open'"), nullable=False,
    )
    #: The same four levels tasks use, so "critical" means one thing across the
    #: module rather than two.
    priority: Mapped[str] = mapped_column(
        String(12), default=TaskPriority.MEDIUM,
        server_default=text("'medium'"), nullable=False,
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    raised_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    raised_on: Mapped[date] = mapped_column(Date, nullable=False)
    due_on: Mapped[date | None] = mapped_column(Date)
    resolved_on: Mapped[date | None] = mapped_column(Date)

    #: Escalates this into the "support needed" box on the next status report.
    needs_support: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: What is actually being asked for. The one field a manager reads the
    #: issues section to find, so it is stored rather than left inside
    #: ``detail`` where a summary would have to guess at it.
    support_note: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="issues")
    owner: Mapped[User | None] = relationship(foreign_keys=[owner_id], lazy="joined")

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_ISSUE_STATUSES

    def __repr__(self) -> str:
        return f"<ProjectIssue {self.title[:40]!r} {self.status}>"


class ProjectUpdate(Base, UUIDPrimaryKey, Timestamped):
    """One movement, recorded as it happened.

    Append-only, and never edited. This is the table that makes a report over
    an arbitrary window possible: "what happened between these two dates" is a
    range scan here, and it is the same query whether the window is a day, a
    week, a month or a year. Deriving the same answer from the current state of
    tasks would give whatever is true now rather than what was true then, which
    is the one thing a report of a past period must not do.

    The before/after columns are nullable because not every update moves a
    number — a note is an update, and so is raising an issue.
    """

    __tablename__ = "project_updates"
    __table_args__ = (
        # Every read is "this project, over this window, newest first".
        Index("ix_project_updates_project_created", "project_id", "created_at"),
        Index("ix_project_updates_task", "task_id"),
        Index("ix_project_updates_author", "author_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    #: The task that moved, when one did. ``SET NULL`` so the history of a
    #: deleted task survives as project history — somebody deleting a task
    #: should not silently rewrite what last month's report was based on.
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_tasks.id", ondelete="SET NULL")
    )
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_milestones.id", ondelete="SET NULL")
    )
    issue_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_issues.id", ondelete="SET NULL")
    )

    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    #: What the thing was called at the time. Copied rather than joined for the
    #: same reason the delivery log copies names: the record has to still read
    #: after its subject is gone or renamed.
    subject: Mapped[str | None] = mapped_column(String(500))

    percent_before: Mapped[int | None] = mapped_column(Integer)
    percent_after: Mapped[int | None] = mapped_column(Integer)
    status_before: Mapped[str | None] = mapped_column(String(16))
    status_after: Mapped[str | None] = mapped_column(String(16))
    hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))

    #: What the person said about it. The part a report quotes.
    body: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="updates")
    author: Mapped[User | None] = relationship(foreign_keys=[author_id], lazy="joined")

    @property
    def percent_delta(self) -> int | None:
        if self.percent_before is None or self.percent_after is None:
            return None
        return self.percent_after - self.percent_before

    def __repr__(self) -> str:
        return f"<ProjectUpdate {self.kind} {self.subject!r}>"
