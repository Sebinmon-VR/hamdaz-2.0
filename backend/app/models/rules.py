"""Rules engine persistence (§5.2-5.4).

Root cause #9: the legacy system hardcodes its assignment policy in ``swp()`` and
``calculate_priority_score()``, so changing how work is distributed needs a developer and a
deploy. These tables move that policy into data an admin edits.

Three properties are requirements, not extras, and each has a table behind it:

* **Versioned** — ``rule_set_versions`` snapshots every publish, so any version reverts.
* **Explainable** — ``rule_evaluations`` records the facts, the matches and the outcome, so
  "why did Rahul get this proposal?" has an answer.
* **Simulatable** — evaluations carry a ``simulated`` flag, so a dry run leaves a full trace
  without taking effect.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey


class DistributionMode(StrEnum):
    """How :mod:`app.services.assignment` picks a winner."""

    LEAST_LOADED = "least_loaded"
    ROUND_ROBIN = "round_robin"
    RATIO = "ratio"
    WEIGHTED_LEAST_LOADED = "weighted_least_loaded"
    MANUAL = "manual"


class RuleSet(Base, UUIDPrimaryKey, Timestamped):
    """An ordered collection of rules bound to one decision point."""

    __tablename__ = "rule_sets"
    __table_args__ = (
        UniqueConstraint("decision_point", "team_id", "name", name="uq_rule_sets_dp_team_name"),
        Index("ix_rule_sets_dp_team", "decision_point", "team_id"),
    )

    #: A key from :data:`app.core.rules.registry.DECISION_POINTS`.
    decision_point: Mapped[str] = mapped_column(String(60), nullable=False)
    #: NULL means org-wide; a team's own set takes precedence over the org default.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Higher wins when several sets match. Ties break on team-scoped before org-wide.
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: When true every matching rule fires; otherwise the first match wins.
    evaluate_all: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rules: Mapped[list[Rule]] = relationship(
        back_populates="rule_set",
        cascade="all, delete-orphan",
        order_by="Rule.position",
        lazy="selectin",
    )

    @property
    def is_published(self) -> bool:
        return self.published_at is not None and self.version > 0

    def __repr__(self) -> str:
        return f"<RuleSet {self.decision_point}:{self.name} v{self.version}>"


class Rule(Base, UUIDPrimaryKey, Timestamped):
    """``conditions → actions``, evaluated in ``position`` order."""

    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("rule_set_id", "position", name="uq_rules_set_position"),)

    rule_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rule_sets.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: ``{"all": [...]}`` / ``{"any": [...]}`` / ``{"always": true}`` — see the evaluator.
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: ``[{"type": "...", ...}, ...]`` validated against the decision point's action schema.
    actions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    rule_set: Mapped[RuleSet] = relationship(back_populates="rules")

    def __repr__(self) -> str:
        return f"<Rule {self.position}:{self.name}>"


class RuleSetVersion(Base, UUIDPrimaryKey):
    """An immutable snapshot taken at publish. This is what makes revert a one-click action."""

    __tablename__ = "rule_set_versions"
    __table_args__ = (
        UniqueConstraint("rule_set_id", "version", name="uq_rule_set_versions_set_version"),
    )

    rule_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rule_sets.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The complete rule set as it was, so a revert needs nothing else.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<RuleSetVersion {self.rule_set_id} v{self.version}>"


class RuleEvaluation(Base, UUIDPrimaryKey):
    """One evaluation, with everything needed to explain it afterwards.

    The policy-side twin of ``audit_log``: that answers *who did this*, this answers *why did
    the system do this*. Between them the developer panel can reconstruct any decision.
    """

    __tablename__ = "rule_evaluations"
    __table_args__ = (
        Index("ix_rule_evaluations_dp_created", "decision_point", "created_at"),
        Index("ix_rule_evaluations_entity", "entity_type", "entity_id"),
    )

    decision_point: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    rule_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rule_sets.id", ondelete="SET NULL")
    )
    rule_set_version: Mapped[int | None] = mapped_column(Integer)
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    #: The input the engine saw. Without this an explanation is just an assertion.
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    matched_rule_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    #: Per-rule detail: which conditions passed, which failed, and why.
    trace: Mapped[list[Any] | None] = mapped_column(JSONB)
    outcome: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: True for a dry run: full trace recorded, nothing applied.
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<RuleEvaluation {self.decision_point} simulated={self.simulated}>"


class AssignmentPolicy(Base, UUIDPrimaryKey, Timestamped):
    """The ``proposal.assign`` policy (§5.4).

    Kept as its own table rather than a generic rule set because it is the most-used policy
    in the system and has a fixed, well-understood shape — eligibility, capacity,
    distribution. That shape is what lets the admin UI offer a purpose-built builder with a
    live preview instead of a generic condition editor.
    """

    __tablename__ = "assignment_policies"
    __table_args__ = (
        UniqueConstraint("team_id", "name", name="uq_assignment_policies_team_name"),
        Index("ix_assignment_policies_team_active", "team_id", "active"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    #: Hard filters: ``max_open_tasks``, ``not_labelled``, ``requires_labels``, …
    eligibility: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: ``{"default": 1.0, "by_label": {"new_joiner": 0.4}}`` — how a new joiner gets less work.
    capacity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: ``{"mode": ..., "factors": {...}, "ratio": {...}}``
    distribution: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    tie_break: Mapped[str] = mapped_column(String(32), default="longest_idle", nullable=False)
    #: What happens when nobody is eligible. Work is never silently dropped.
    fallback: Mapped[str] = mapped_column(String(32), default="notify_manager", nullable=False)
    allow_manual_override: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<AssignmentPolicy {self.name} v{self.version} active={self.active}>"


class AssignmentPolicyVersion(Base, UUIDPrimaryKey):
    __tablename__ = "assignment_policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_assignment_policy_versions"),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assignment_policies.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
