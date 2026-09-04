"""Per-person workload analytics and the priority score built from them.

A *run* is one computation: task counts read live from the Proposals list,
combined with each person's labels and the assignment policy, producing a ranked
score per person. Runs are kept rather than overwritten, so "why did Rahul get
that proposal in March" has an answer in April.

**Everything is stored here, in Postgres.** Nothing is written to SharePoint —
the Proposals list is read (every call a GET) and the ``testuseranalytics`` list
is not touched at all. Publishing there is a separate decision for later.

Two things this table deliberately keeps that a bare score would not:

* **the inputs**, not just the output. Task counts, capacity and the labels that
  produced it are frozen onto the row, because the score is meaningless a month
  later if the numbers behind it have moved on.
* **the factor breakdown** in ``factors``. A single number nobody can decompose
  is a number nobody will trust, and the first question anyone asks about a
  ranking is which part of it put them there.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User


class AnalyticsRun(Base, UUIDPrimaryKey, Timestamped):
    """One scoring pass over one group of people."""

    __tablename__ = "analytics_runs"
    __table_args__ = (Index("ix_analytics_runs_team_created", "team_id", "created_at"),)

    #: NULL means everyone with proposal work, not scoped to a team.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    team_name: Mapped[str | None] = mapped_column(String(160))
    #: Which policy governed this run. Kept by id *and* by value, because a
    #: policy that is later edited would otherwise silently rewrite history.
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assignment_policies.id", ondelete="SET NULL")
    )
    policy_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    #: How many rows were read to produce this, and from where.
    source: Mapped[str] = mapped_column(String(60), default="sharepoint", nullable=False)
    rows_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: People in the source who were not scored, and why — usually not on the team.
    excluded_note: Mapped[str | None] = mapped_column(Text)

    #: A run that was computed but not kept as the record of a decision.
    #: Previewing must not litter the history with rankings nobody acted on.
    saved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by: Mapped[User | None] = relationship(
        foreign_keys=[created_by_id], lazy="joined"
    )
    entries: Mapped[list[UserAnalytics]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="UserAnalytics.priority_score",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<AnalyticsRun {self.team_name or 'all'} n={len(self.entries)}>"


class UserAnalytics(Base, UUIDPrimaryKey, Timestamped):
    """One person's numbers in one run."""

    __tablename__ = "user_analytics"
    __table_args__ = (
        Index("ix_user_analytics_run_priority", "run_id", "priority_score"),
        Index("ix_user_analytics_user", "user_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analytics_runs.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL for somebody who holds proposal work but has never signed in here.
    #: They still get counted — leaving them out would understate the team's load.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    #: Their id inside the SharePoint site, for tracing a count back to rows.
    sharepoint_lookup_id: Mapped[str | None] = mapped_column(String(40))

    # ── the counts, live from the Proposals list ───────────────────────
    total_tasks: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    open_tasks: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    completed_tasks: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    overdue_tasks: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    due_soon_tasks: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: Counted separately because a fifth of the list has no status at all, and
    #: those rows land in ``open_tasks`` — a reader should be able to see how
    #: much of somebody's load is that rather than real work in progress.
    no_status_tasks: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: Never given a status and the bid has closed, so read as finished rather
    #: than as live work. Kept separate so a suspicious count can be traced.
    expired_tasks: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: Not finished, but the bid closed. Not counted as current workload.
    bid_closed_tasks: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: Not finished and the bid is today or later. **What the score is built on.**
    active_tasks: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    #: When they were last given work, from the newest row assigned to them.
    last_assigned_on: Mapped[date | None] = mapped_column(DateTime(timezone=True))
    days_since_last_assign: Mapped[int | None] = mapped_column(Integer)

    # ── what the policy made of them ───────────────────────────────────
    labels: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    capacity: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal(1), nullable=False)
    #: ``open_tasks / capacity`` — the number that makes a ratio work.
    effective_load: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal(0), nullable=False
    )
    max_open: Mapped[int | None] = mapped_column(Integer)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    excluded_reason: Mapped[str | None] = mapped_column(Text)

    # ── the result ─────────────────────────────────────────────────────
    #: **1, 2, 3, 4 ... where 1 is the highest priority** — next in line for
    #: work. Consecutive over the assignable people, no gaps. NULL when
    #: excluded: out of the queue is not the same as last in it, and a number
    #: would invite somebody to sort by it and assign to them anyway.
    priority_score: Mapped[int | None] = mapped_column(Integer)
    #: The sum of the weighted factor contributions, 0..1. **Not a score.** The
    #: score is ``priority_score``, which is 1, 2, 3 ... This is here only to
    #: show how far apart two positions are: first and second can be separated by
    #: a hair or by a mile, and 1 and 2 look identical either way. Named without
    #: "score" in it deliberately — the two were confused when it was not.
    factor_total: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    #: Per factor: raw value, normalised value and weighted contribution. This
    #: is what makes a ranking arguable instead of merely announced.
    factors: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    run: Mapped[AnalyticsRun] = relationship(back_populates="entries")

    def __repr__(self) -> str:
        return f"<UserAnalytics {self.display_name} score={self.priority_score}>"
