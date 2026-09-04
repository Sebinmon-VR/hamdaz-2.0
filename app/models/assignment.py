"""The assignment policy: how work should be shared out, as data rather than code.

The problem this exists to solve is that the legacy system hardcoded its
distribution, so changing who gets what needed a developer and a deploy. Here it
is a row an admin edits.

**Ratios are capacity multipliers, not counters.** "New joiners get one task for
every two" is expressed as a capacity of ``0.5``, and load is then judged as
``open_tasks / capacity``. A new joiner holding 2 scores like someone holding 4,
so they naturally fall to the back of the queue at half the rate — and it works
the same for a senior on ``1.5`` without either being a special case in code. A
literal counter would need to know what "every two" was counted against, would
drift whenever somebody was skipped, and would have nothing sensible to say
about three different labels at once.

One policy per team, plus an org-wide default with ``team_id`` NULL that a team
without its own falls back to.

This module holds the *settings*. Nothing here scores anybody — that comes next,
and keeping it apart means the policy can be edited and reviewed before any of it
starts moving work around.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
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

#: Anyone at or below this is effectively unassignable. Also guards the division.
MIN_CAPACITY = Decimal("0.01")

#: What a person with no capacity-bearing label gets.
DEFAULT_CAPACITY = Decimal("1.0")


class AssignmentPolicy(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "assignment_policies"
    __table_args__ = (
        # One per team, and exactly one org-wide default. Postgres treats NULLs
        # as distinct in a unique index, so the default is guarded separately by
        # a partial index in the migration.
        UniqueConstraint("team_id", name="uq_assignment_policy_team"),
        CheckConstraint(
            "weight_load >= 0 AND weight_open_count >= 0 AND weight_idle_days >= 0",
            name="ck_assignment_weights_non_negative",
        ),
        CheckConstraint("new_joiner_days >= 0", name="ck_assignment_new_joiner_days"),
    )

    #: NULL is the org-wide default, used by any team without its own.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="Assignment policy")
    description: Mapped[str | None] = mapped_column(Text)
    #: Off means this policy is not consulted; the org default applies instead.
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    # ── capacity ───────────────────────────────────────────────────────
    #: For anyone holding no label named in ``capacity_by_label``.
    default_capacity: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), default=DEFAULT_CAPACITY, server_default=text("1.0"), nullable=False
    )
    #: ``{"new-joiner": 0.5, "senior": 1.5}``. Holding several, the lowest wins:
    #: a senior who is also in training should be treated as in training.
    capacity_by_label: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    # ── hard limits ────────────────────────────────────────────────────
    #: A ceiling regardless of how the scoring comes out. NULL means no ceiling.
    default_max_open: Mapped[int | None] = mapped_column(Integer)
    #: ``{"new-joiner": 4}``. Again the lowest applies.
    max_open_by_label: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    #: Label keys that take someone out of the pool entirely.
    excluded_labels: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    #: Roles whose holders are never given work. Managers run the queue rather
    #: than stand in it, so by default they are not scored at all — a manager
    #: quietly ranking first is how a team ends up wondering why their lead has
    #: twelve proposals. Editable, because a working team lead is a real thing.
    excluded_roles: Mapped[list[Any]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("""'["manager", "team_manager"]'::jsonb"""),
        nullable=False,
    )
    #: Skip anyone on approved leave today. Derived from the leave module rather
    #: than from a stored label, so somebody returns to the pool the day their
    #: leave ends without anyone remembering to do anything.
    exclude_on_leave: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    # ── automatic labels ───────────────────────────────────────────────
    #: Someone whose ``joined_on`` is within this many days counts as a new
    #: joiner without anyone assigning the label, and stops counting as one on
    #: the day it lapses. 0 turns the rule off.
    new_joiner_days: Mapped[int] = mapped_column(
        Integer, default=90, server_default=text("90"), nullable=False
    )
    #: Whether somebody with no joining date should be treated as a new joiner
    #: based on when they first appeared in this system.
    #:
    #: **Off by design.** With it on and no joining dates recorded, everybody
    #: looks like they joined when the ERP was installed, so every capacity
    #: collapses to the new-joiner value and the ratio stops distinguishing
    #: anyone — which is worse than having no rule at all. Absence of evidence
    #: should not halve somebody's workload. Turn it on once joining dates are
    #: set and it becomes a reasonable stand-in for the few who are missing one.
    new_joiner_from_first_seen: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    # ── scoring weights ────────────────────────────────────────────────
    #: How much each factor counts when ranking candidates. Held as separate
    #: columns rather than a blob so each can be constrained and shown as its
    #: own control. They are normalised at read time, so they need not sum to 1.
    weight_load: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), default=Decimal("0.45"), server_default=text("0.45"), nullable=False
    )
    weight_open_count: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), default=Decimal("0.30"), server_default=text("0.30"), nullable=False
    )
    #: Rewards whoever has waited longest — what stops the same two people
    #: absorbing everything because they happen to close work quickly.
    weight_idle_days: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), default=Decimal("0.25"), server_default=text("0.25"), nullable=False
    )

    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    team: Mapped[Team | None] = relationship(lazy="joined")
    updated_by: Mapped[User | None] = relationship(
        foreign_keys=[updated_by_id], lazy="joined"
    )

    @property
    def is_default(self) -> bool:
        return self.team_id is None

    def __repr__(self) -> str:
        scope = "org" if self.is_default else f"team={self.team_id}"
        return f"<AssignmentPolicy {scope} enabled={self.enabled}>"
