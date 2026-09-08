"""Team reports: what each team files, and what it said.

A report is a person's account of a period of work — a day, a week, a month —
filed to their team's managers and readable by the CEO and super admins. The
shape is fixed and the content is not: six sections every report has, and
whatever else that particular team is asked for on top.

**The skeleton is code; the questions are data.** Overview, tasks, issues,
remarks, metrics and summary are structural — the reader of a presales report
and the reader of a purchasing one should not have to work out where the issues
are. What each team is *asked* inside those sections is a ``FormTemplate``, so
adding "quotes sent this week" to the presales daily report is an edit rather
than a deploy. That is the same arrangement HR uses for job applications, and
it is here for the same reason.

**The parts that are worth querying get tables.** Tasks, issues and metrics are
rows rather than JSONB, because the questions people will actually ask are
across reports and not within one: what is blocking presales this month, how
has the conversion rate moved, which tasks keep reappearing unfinished. Free
text — the overview, the remarks, the summary, and the team's own custom fields
— stays as text and JSONB, because nobody queries prose.

**Submitting is delivery, not a request.** A report goes from draft to
submitted and stops. Managers read it and may comment; nothing waits on their
decision, because a daily report is a record of what happened rather than
something that needs approving. What a manager disagrees with, they say in a
comment, and the report still stands as what was reported at the time.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.team import Team
from app.models.templates import FormTemplate
from app.models.user import User


class ReportCadence(StrEnum):
    """How often a report is filed. Also decides what period one covers."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    #: Filed when there is something to file — a bid post-mortem, a site visit.
    #: Its period is whatever the author says, because nothing else can know.
    AD_HOC = "ad_hoc"


class ReportStatus(StrEnum):
    #: The author's, and nobody else's. Not visible to a manager: a half-written
    #: report read as a finished one is how people learn not to draft in the tool.
    DRAFT = "draft"
    #: Filed. Read-only to its author, visible to the people it goes to.
    SUBMITTED = "submitted"


class TaskSource(StrEnum):
    #: Pulled from the SharePoint Proposals list and snapshotted at that moment.
    PROPOSALS = "proposals"
    #: Typed in. Work that lives nowhere else, which is most of what people do.
    MANUAL = "manual"


class IssueSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    #: Somebody is stopped and cannot proceed. Deliberately its own level rather
    #: than the top of a scale: "blocked" is a state, not an intensity, and it
    #: is the one a manager reads the report to find.
    BLOCKED = "blocked"


class ReportSettings(Base, Timestamped):
    """The super admin's switches for reporting. One row; ``id`` is fixed at 1.

    Who a filed report goes to used to be a constant in the code — the team's
    managers and leads, the CEO, super admins — which was the right default and
    the wrong thing to be unchangeable. An organisation that wants the CEO off
    every daily report, or the whole of finance on the weeklies, should not need
    a deploy to say so.

    What this cannot do is widen who may *read* a report. Delivery and
    visibility are separate questions and only one of them is configurable here:
    adding an address to ``extra_recipients`` mails them a summary; it does not
    let them open the report, and the link in the message will refuse them like
    any other. That separation is deliberate. A settings screen that could
    silently grant read access to everyone's reports is a settings screen
    somebody will use by accident.
    """

    __tablename__ = "report_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    #: The master switch for the email. Off leaves reports filing normally and
    #: simply tells nobody — which is what a company piloting the module wants
    #: before it starts landing in the CEO's inbox.
    notify_on_submit: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: Mail the team's own managers and leads. On: they are the people the
    #: report is actually written for.
    notify_team_oversight: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: Mail everyone holding one of ``company_roles``. On by default because
    #: that was the shipped behaviour, and off is one switch away.
    notify_company_wide: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: Which global roles count as company-wide *for the email*. Defaults to
    #: the same three that may read every report, but is its own list: a CEO
    #: who wants to keep the access and lose the daily mail changes this and
    #: nothing else.
    company_roles: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)),
        default=lambda: ["super_admin", "ceo", "manager"],
        server_default=text("""'{super_admin,ceo,manager}'::varchar[]"""),
        nullable=False,
    )
    #: Addresses always copied, for people who are not users here — an external
    #: consultant, a shared mailbox the team files into.
    extra_recipients: Mapped[list[str]] = mapped_column(
        ARRAY(String(320)), default=list, server_default=text("'{}'::varchar[]"),
        nullable=False,
    )
    #: Send the author their own report back. Off: nobody needs a copy of what
    #: they just wrote, and it is the fastest way to teach people that these
    #: messages are noise.
    copy_author: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Which cadences are mailed at all. The common ask is weeklies and
    #: monthlies but not dailies — a manager of six people would otherwise get
    #: thirty messages a week and read none of them.
    notify_cadences: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)),
        default=lambda: ["daily", "weekly", "monthly", "ad_hoc"],
        server_default=text("""'{daily,weekly,monthly,ad_hoc}'::varchar[]"""),
        nullable=False,
    )
    #: How much of the report the message carries before it stops being a
    #: summary and becomes something nobody reads past the first screen.
    max_tasks_in_email: Mapped[int] = mapped_column(
        Integer, default=8, server_default=text("8"), nullable=False
    )
    include_task_list: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    include_issue_list: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: How long delivery records are worth keeping, for the admin log below.
    #: Advisory: nothing deletes automatically, it is what the screen defaults
    #: its date range to.
    log_retention_days: Mapped[int] = mapped_column(
        Integer, default=90, server_default=text("90"), nullable=False
    )

    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        return f"<ReportSettings notify={self.notify_on_submit}>"


class DeliveryStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    #: Nothing was attempted, and why: the switch is off, this cadence is not
    #: mailed, or there was nobody to send to. Recorded rather than left blank
    #: so "why did my manager not get it" has an answer that is not a guess.
    SKIPPED = "skipped"


class ReportDelivery(Base, UUIDPrimaryKey, Timestamped):
    """One attempt to mail one report, and what came of it.

    A log rather than only the three columns on ``Report`` because the question
    a super admin asks is across reports — "has anything failed to send this
    week", "is the CEO actually on these" — and a per-row summary cannot answer
    either. The columns on the report stay, because the other question people
    ask is about one particular report and a log is the wrong shape for that.

    Readable by a super admin and nobody else. It holds who was mailed about
    whom, which is a map of the organisation's reporting lines and not
    something an ordinary person needs.
    """

    __tablename__ = "report_deliveries"
    __table_args__ = (
        Index("ix_report_deliveries_created", "created_at"),
        Index("ix_report_deliveries_report", "report_id"),
    )

    report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="SET NULL")
    )
    #: Copied off the report rather than joined, so the log still reads after a
    #: report is deleted. A delivery record whose subject has vanished is
    #: exactly the record somebody is trying to look up.
    team_name: Mapped[str | None] = mapped_column(String(200))
    author_name: Mapped[str | None] = mapped_column(String(200))
    cadence: Mapped[str | None] = mapped_column(String(16))
    period_start: Mapped[date | None] = mapped_column(Date)

    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    recipients: Mapped[list[str]] = mapped_column(
        ARRAY(String(320)), default=list, server_default=text("'{}'::varchar[]"),
        nullable=False,
    )
    #: Why nothing was sent, or why it failed. Null on a clean send.
    detail: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<ReportDelivery {self.status} {self.report_id}>"


class ReportSchedule(Base, UUIDPrimaryKey, Timestamped):
    """Which template one team uses for one cadence.

    This is what makes each team's report different. Presales' daily report and
    purchasing's daily report are two ``FormTemplate`` rows; this table is how
    the system knows which is which without the template having to name a team.

    A team with no schedule for a cadence simply cannot file that report — an
    absence rather than a refusal, so nobody is asked for a weekly report their
    manager never set up.
    """

    __tablename__ = "report_schedules"
    __table_args__ = (
        UniqueConstraint("team_id", "cadence", name="uq_report_schedule_team_cadence"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cadence: Mapped[str] = mapped_column(String(16), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("form_templates.id", ondelete="RESTRICT"), nullable=False
    )
    #: Off keeps the schedule and its history while stopping new reports, which
    #: is what "we have paused weeklies" means. Deleting it would lose which
    #: template last applied.
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: When it is expected, local to whoever reads it: an hour of the day for a
    #: daily, and for a weekly the weekday too (0 = Monday). Advisory — nothing
    #: is refused for being late, it is only how "overdue" is worked out.
    due_hour: Mapped[int] = mapped_column(
        Integer, default=18, server_default=text("18"), nullable=False
    )
    due_weekday: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)

    #: Mail this team's reports of this cadence. Null follows the global
    #: setting; false silences just this one. Three states rather than two so
    #: that turning the global switch on later reaches a team that never
    #: expressed a preference, and does not reach one that said no.
    notify: Mapped[bool | None] = mapped_column(Boolean)
    #: Addresses copied on this team's reports only — the account manager who
    #: cares about presales and nothing else. Added to whatever the global
    #: settings already produce; this narrows nothing.
    extra_recipients: Mapped[list[str]] = mapped_column(
        ARRAY(String(320)), default=list, server_default=text("'{}'::varchar[]"),
        nullable=False,
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    team: Mapped[Team] = relationship(lazy="joined")
    template: Mapped[FormTemplate] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<ReportSchedule {self.team_id} {self.cadence}>"


class Report(Base, UUIDPrimaryKey, Timestamped):
    """One person's report on one period, for one team."""

    __tablename__ = "reports"
    __table_args__ = (
        # One person files one report per period per team. Two daily reports for
        # the same Tuesday is a mistake every time, and catching it here is
        # kinder than letting a manager read both and wonder which is current.
        UniqueConstraint(
            "team_id", "author_id", "cadence", "period_start",
            name="uq_report_author_period",
        ),
        Index("ix_reports_team_period", "team_id", "period_start"),
        Index("ix_reports_author_period", "author_id", "period_start"),
        Index("ix_reports_status", "status"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The definition this was filled in against, and the version of it. Both,
    #: because a template is edited and a report filed last month has to stay
    #: readable against what it actually asked.
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("form_templates.id", ondelete="RESTRICT"), nullable=False
    )
    template_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )

    cadence: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Inclusive both ends. A daily report has the same date twice, which keeps
    #: every query over a range written one way instead of two.
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default=ReportStatus.DRAFT, server_default=text("'draft'"), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: When the report was emailed to the people it goes to, and to how many.
    #: Kept on the row rather than only in a log because the question people
    #: ask is always about one particular report — "did my manager get
    #: Tuesday's?" — and a log is the wrong shape to answer that.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: What went wrong, when something did. A submission is never failed by a
    #: failure to send — the report is filed either way — but a silent failure
    #: hides the one fact that matters, which is that nobody was told.
    notify_error: Mapped[str | None] = mapped_column(Text)

    #: The three prose sections. Separate columns rather than keys in ``answers``
    #: because every report has them whatever its template says, and because
    #: "show me the summaries for last week" should not have to reach into JSONB.
    overview: Mapped[str | None] = mapped_column(Text)
    remarks: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)

    #: The team's own questions, keyed by template field. Validated against the
    #: template on the way in, exactly as HR validates an application.
    answers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    team: Mapped[Team] = relationship(lazy="joined")
    author: Mapped[User] = relationship(foreign_keys=[author_id], lazy="joined")
    template: Mapped[FormTemplate] = relationship(foreign_keys=[template_id], lazy="joined")

    tasks: Mapped[list[ReportTaskLine]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportTaskLine.position",
        lazy="selectin",
    )
    issues: Mapped[list[ReportIssue]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportIssue.position",
        lazy="selectin",
    )
    metrics: Mapped[list[ReportMetric]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportMetric.position",
        lazy="selectin",
    )

    @property
    def is_editable(self) -> bool:
        return self.status == ReportStatus.DRAFT

    def __repr__(self) -> str:
        return f"<Report {self.cadence} {self.period_start} {self.author_id} {self.status}>"


class ReportTaskLine(Base, UUIDPrimaryKey, Timestamped):
    """One task on a report, and how far along it is.

    Rows from the Proposals list are **snapshotted, not referenced**. The list
    is live: a task renamed or reassigned next month would silently rewrite what
    somebody reported this month, and a report that changes after it is filed is
    not a report. The link back to SharePoint stays live, so anybody wanting the
    current state is one click from it.

    Nothing here is ever written back to SharePoint. Reports read that list and
    only read it.
    """

    __tablename__ = "report_task_lines"
    __table_args__ = (Index("ix_report_task_lines_report", "report_id", "position"),)

    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(
        String(16), default=TaskSource.MANUAL, server_default=text("'manual'"), nullable=False
    )
    #: The SharePoint item id, for a pulled row. Kept so the same task can be
    #: recognised across reports — "this bid has been on his report for a
    #: fortnight" is a question worth being able to answer.
    external_id: Mapped[str | None] = mapped_column(String(64), index=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    #: What the source system calls it, as it was at the time.
    status: Mapped[str | None] = mapped_column(String(80))
    #: What the author says, which is the point of the section. A pulled status
    #: says whether SharePoint has been updated; this says whether the work is
    #: done, and those are not the same claim.
    completion: Mapped[str] = mapped_column(
        String(24), default="in_progress", server_default=text("'in_progress'"), nullable=False
    )
    #: 0-100. Optional: a percentage nobody maintains is worse than none.
    percent_complete: Mapped[int | None] = mapped_column(Integer)

    priority: Mapped[str | None] = mapped_column(String(40))
    end_user: Mapped[str | None] = mapped_column(String(200))
    quote_no: Mapped[str | None] = mapped_column(String(80))
    deadline: Mapped[date | None] = mapped_column(Date)

    #: Where the work actually lives. For a pulled row, the SharePoint display
    #: form; for a typed one, whatever the author pasted.
    link: Mapped[str | None] = mapped_column(Text)
    #: Straight to the item's files. Opening it uses the reader's own SharePoint
    #: access, so the link widens nothing.
    attachments_url: Mapped[str | None] = mapped_column(Text)
    has_attachments: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: The author's own words about this task.
    note: Mapped[str | None] = mapped_column(Text)

    report: Mapped[Report] = relationship(back_populates="tasks")

    @property
    def is_done(self) -> bool:
        return self.completion == "done"

    def __repr__(self) -> str:
        return f"<ReportTaskLine {self.title[:30]!r} {self.completion}>"


class ReportIssue(Base, UUIDPrimaryKey, Timestamped):
    """Something in the way. A row rather than prose so it can be counted."""

    __tablename__ = "report_issues"
    __table_args__ = (Index("ix_report_issues_report", "report_id", "position"),)

    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(
        String(16), default=IssueSeverity.MEDIUM, server_default=text("'medium'"), nullable=False
    )
    #: Who or what it is waiting on — a supplier, a customer, another team.
    #: Free text on purpose: most of what blocks a bid is outside this system.
    waiting_on: Mapped[str | None] = mapped_column(String(200))
    #: Still open on this report. A closed one is kept, because "raised and
    #: resolved the same week" is a thing worth being able to see.
    resolved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: The task it concerns, when it concerns one.
    task_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("report_task_lines.id", ondelete="SET NULL")
    )

    report: Mapped[Report] = relationship(back_populates="issues")

    def __repr__(self) -> str:
        return f"<ReportIssue {self.severity} {self.title[:30]!r}>"


class ReportMetric(Base, UUIDPrimaryKey, Timestamped):
    """One number on a report, and where it came from.

    Both values are kept. ``computed`` is what the task data said when the
    report was drawn up; ``value`` is what the author put. Storing only the
    final figure would lose the more interesting fact — that somebody corrected
    it — and storing only the computed one would make the section read-only,
    which it must not be: the data behind it is a live SharePoint list that is
    often behind what the person actually did.
    """

    __tablename__ = "report_metrics"
    __table_args__ = (
        UniqueConstraint("report_id", "key", name="uq_report_metric_key"),
        Index("ix_report_metrics_key", "key"),
    )

    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(24))
    #: What the task data said. Null for a metric nothing can compute.
    computed: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    #: What the author put. Null means they left the computed figure alone.
    value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    target: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    report: Mapped[Report] = relationship(back_populates="metrics")

    @property
    def effective(self) -> Decimal | None:
        """The figure that counts: what the author put, else what was computed."""
        return self.value if self.value is not None else self.computed

    @property
    def edited(self) -> bool:
        """Whether a person moved it off the computed figure."""
        return (
            self.value is not None
            and self.computed is not None
            and self.value != self.computed
        )

    def __repr__(self) -> str:
        return f"<ReportMetric {self.key}={self.effective}>"


class ReportComment(Base, UUIDPrimaryKey, Timestamped):
    """A reader's remark on a submitted report.

    Not a decision. Nothing about the report changes because of one, which is
    the difference between this and a quote-request review: a report says what
    happened, and a manager disagreeing with it says so beside it rather than
    sending it back to be rewritten.
    """

    __tablename__ = "report_comments"
    __table_args__ = (Index("ix_report_comments_report", "report_id", "created_at"),)

    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)

    author: Mapped[User] = relationship(foreign_keys=[author_id], lazy="joined")

    def __repr__(self) -> str:
        return f"<ReportComment {self.report_id} {self.author_id}>"


class ReportRead(Base, UUIDPrimaryKey, Timestamped):
    """That a particular person has read a particular report.

    Worth recording because the complaint reports attract is always the same
    one — "nobody reads them". This is how that is answered with a fact.
    """

    __tablename__ = "report_reads"
    __table_args__ = (UniqueConstraint("report_id", "user_id", name="uq_report_read"),)

    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")

    def __repr__(self) -> str:
        return f"<ReportRead {self.report_id} {self.user_id}>"
