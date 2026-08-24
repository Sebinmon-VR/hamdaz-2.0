"""Binding the assignment engine to real data (§5.4).

The engine in :mod:`app.core.rules.assignment` is pure. This module builds its inputs from
the database, applies the result, and records why — so `preview` and `assign` share one code
path and the preview cannot drift from reality.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, lazyload

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.principal import Principal
from app.core.rules.assignment import (
    DEFAULT_POLICY,
    AssignmentDecision,
    Candidate,
    decide,
)
from app.models.identity import Membership, User, UserStatus
from app.models.labels import LabelAssignment
from app.models.leave import LeaveRequest, LeaveStatus
from app.models.platform import AuditAction
from app.models.proposals import (
    OPEN_STATUS_VALUES,
    Proposal,
    ProposalEvent,
    ProposalEventType,
    ProposalStatus,
)
from app.models.rules import AssignmentPolicy, AssignmentPolicyVersion
from app.services import audit_service, rules_service

logger = get_logger(__name__)

RATIO_WINDOW_DAYS = 30


async def get_active_policy(
    session: AsyncSession, team_id: uuid.UUID
) -> AssignmentPolicy | None:
    policy: AssignmentPolicy | None = await session.scalar(
        select(AssignmentPolicy).where(
            AssignmentPolicy.team_id == team_id, AssignmentPolicy.active.is_(True)
        )
    )
    return policy


def policy_config(policy: AssignmentPolicy | None) -> dict[str, Any]:
    """The policy as plain config, falling back to the shipped default.

    A team that has never configured anything still gets sensible behaviour rather than an
    error — that is what makes the system usable on day one.
    """
    if policy is None:
        return dict(DEFAULT_POLICY)
    return {
        "eligibility": policy.eligibility or DEFAULT_POLICY["eligibility"],
        "capacity": policy.capacity or DEFAULT_POLICY["capacity"],
        "distribution": policy.distribution or DEFAULT_POLICY["distribution"],
        "tie_break": policy.tie_break,
        "fallback": policy.fallback,
    }


async def build_candidates(
    session: AsyncSession, team_id: uuid.UUID, *, now: datetime | None = None
) -> list[Candidate]:
    """Every active member of a team, with the load and label data the engine needs.

    Three aggregates in three queries rather than one per member: the legacy code did the
    equivalent in a pandas loop over the whole proposal list on every request.
    """
    now = now or datetime.now(UTC)
    window_start = now - timedelta(days=RATIO_WINDOW_DAYS)

    # lazyload("*") matters more than it looks. User.memberships, User.label_assignments,
    # Role.permissions and Membership.role are all lazy="selectin" on the models, so loading
    # a user here otherwise drags in their memberships, roles, role permissions and labels —
    # none of which this function reads. It only wants id, display_name and status.
    memberships = (
        (
            await session.scalars(
                select(Membership)
                .where(Membership.team_id == team_id)
                .options(lazyload("*"), joinedload(Membership.user))
            )
        )
        .unique()
        .all()
    )

    users = [
        m.user for m in memberships if m.user is not None and m.user.status is UserStatus.ACTIVE
    ]
    if not users:
        return []

    user_ids = [u.id for u in users]

    open_counts: dict[uuid.UUID, int] = dict(
        (
            await session.execute(
                select(Proposal.assigned_to, func.count())
                .where(
                    Proposal.assigned_to.in_(user_ids),
                    Proposal.status.in_(OPEN_STATUS_VALUES),
                )
                .group_by(Proposal.assigned_to)
            )
        ).all()  # type: ignore[arg-type]
    )

    last_assigned: dict[uuid.UUID, datetime | None] = dict(
        (
            await session.execute(
                select(Proposal.assigned_to, func.max(Proposal.assigned_at))
                .where(Proposal.assigned_to.in_(user_ids))
                .group_by(Proposal.assigned_to)
            )
        ).all()  # type: ignore[arg-type]
    )

    recent_counts: dict[uuid.UUID, int] = dict(
        (
            await session.execute(
                select(Proposal.assigned_to, func.count())
                .where(
                    Proposal.assigned_to.in_(user_ids),
                    Proposal.assigned_at.is_not(None),
                    Proposal.assigned_at >= window_start,
                )
                .group_by(Proposal.assigned_to)
            )
        ).all()  # type: ignore[arg-type]
    )

    label_rows = (
        (
            await session.scalars(
                select(LabelAssignment)
                .where(LabelAssignment.user_id.in_(user_ids))
                .options(lazyload("*"), joinedload(LabelAssignment.label))
            )
        )
        .unique()
        .all()
    )

    labels_by_user: dict[uuid.UUID, set[str]] = {}
    for row in label_rows:
        if row.label is None or not row.is_active(now=now):
            continue
        if row.team_id is not None and row.team_id != team_id:
            continue
        labels_by_user.setdefault(row.user_id, set()).add(row.label.key)

    on_leave = await _users_on_leave(session, user_ids, now)

    return [
        Candidate(
            user_id=user.id,
            display_name=user.display_name,
            labels=frozenset(labels_by_user.get(user.id, set())),
            open_task_count=int(open_counts.get(user.id, 0)),
            last_assigned_at=last_assigned.get(user.id),
            on_leave=user.id in on_leave,
            recent_assignment_count=int(recent_counts.get(user.id, 0)),
        )
        for user in users
    ]


async def _users_on_leave(
    session: AsyncSession, user_ids: list[uuid.UUID], now: datetime
) -> set[uuid.UUID]:
    """Who is away right now, per approved leave that spans this moment."""
    rows = (
        await session.scalars(
            select(LeaveRequest.user_id).where(
                LeaveRequest.user_id.in_(user_ids),
                LeaveRequest.status == LeaveStatus.APPROVED,
                LeaveRequest.start_date <= now,
                LeaveRequest.end_date >= now,
            )
        )
    ).all()
    return set(rows)


async def preview(
    session: AsyncSession,
    *,
    team_id: uuid.UUID,
    required_labels: frozenset[str] = frozenset(),
    count: int = 10,
    policy_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Who would get the next N proposals under a policy.

    "Simulate before publish" for assignment. Each hypothetical assignment increments that
    candidate's in-memory load, so the preview shows the policy *spreading* work rather than
    handing all ten to the same person — which is what actually happens in practice.
    """
    config = policy_override or policy_config(await get_active_policy(session, team_id))
    candidates = await build_candidates(session, team_id)

    if not candidates:
        return {
            "assignments": [],
            "candidates": [],
            "warning": "This team has no active members, so nothing can be assigned.",
        }

    working = {c.user_id: c for c in candidates}
    now = datetime.now(UTC)
    assignments: list[dict[str, Any]] = []

    for index in range(max(1, min(count, 50))):
        decision = decide(
            candidates=list(working.values()),
            eligibility=config["eligibility"],
            capacity_config=config["capacity"],
            distribution=config["distribution"],
            tie_break=config.get("tie_break", "longest_idle"),
            fallback=config.get("fallback", "notify_manager"),
            required_labels=required_labels,
            now=now,
        )

        assignments.append(
            {
                "position": index + 1,
                "assignee_id": str(decision.assignee_id) if decision.assignee_id else None,
                "assignee_name": next(
                    (
                        c.display_name
                        for c in working.values()
                        if c.user_id == decision.assignee_id
                    ),
                    None,
                ),
                "explanation": decision.explanation,
                "fallback": decision.fallback,
            }
        )

        if decision.assignee_id is None:
            break

        chosen = working[decision.assignee_id]
        working[chosen.user_id] = Candidate(
            user_id=chosen.user_id,
            display_name=chosen.display_name,
            labels=chosen.labels,
            open_task_count=chosen.open_task_count + 1,
            last_assigned_at=now,
            on_leave=chosen.on_leave,
            recent_assignment_count=chosen.recent_assignment_count + 1,
        )

    final = decide(
        candidates=candidates,
        eligibility=config["eligibility"],
        capacity_config=config["capacity"],
        distribution=config["distribution"],
        tie_break=config.get("tie_break", "longest_idle"),
        fallback=config.get("fallback", "notify_manager"),
        required_labels=required_labels,
        now=now,
    )

    distribution: dict[str, int] = {}
    for entry in assignments:
        if entry["assignee_name"]:
            distribution[entry["assignee_name"]] = distribution.get(entry["assignee_name"], 0) + 1

    return {
        "assignments": assignments,
        "distribution": distribution,
        "candidates": final.to_json()["ranked"],
        "excluded": final.to_json()["excluded"],
        "policy": config,
    }


async def assign_proposal(
    session: AsyncSession,
    *,
    proposal: Proposal,
    actor: Principal | None = None,
    force_user_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> AssignmentDecision:
    """Assign one proposal, applying the team's policy and recording the decision."""
    now = datetime.now(UTC)

    if force_user_id is not None:
        return await _apply_manual(session, proposal, force_user_id, actor, reason, now)

    config = policy_config(await get_active_policy(session, proposal.team_id))
    candidates = await build_candidates(session, proposal.team_id, now=now)

    required = frozenset(str(x) for x in (proposal.required_labels or ()))

    # Rules run first: they can narrow the pool or stop automatic assignment entirely.
    rule_decision, evaluation = await rules_service.evaluate(
        session,
        decision_point="proposal.assign",
        facts=_assignment_facts(proposal, len(candidates), now),
        team_id=proposal.team_id,
        entity_type="proposal",
        entity_id=str(proposal.id),
        actor=actor,
    )

    if rule_decision.has_action("require_manual_assignment"):
        return AssignmentDecision(
            assignee_id=None,
            mode="manual",
            fallback="manual",
            explanation="A rule requires this proposal to be assigned manually.",
        )

    for action in rule_decision.actions_of_type("assign_to_label"):
        if label := action.get("label"):
            required = required | {str(label)}

    excluded_labels = {
        str(a["label"]) for a in rule_decision.actions_of_type("exclude_label") if a.get("label")
    }
    if excluded_labels:
        eligibility = dict(config["eligibility"])
        eligibility["not_labelled"] = sorted(
            set(eligibility.get("not_labelled") or ()) | excluded_labels
        )
        config = {**config, "eligibility": eligibility}

    decision = decide(
        candidates=candidates,
        eligibility=config["eligibility"],
        capacity_config=config["capacity"],
        distribution=config["distribution"],
        tie_break=config.get("tie_break", "longest_idle"),
        fallback=config.get("fallback", "notify_manager"),
        required_labels=required,
        now=now,
    )

    if decision.assignee_id is not None:
        await _apply(
            session,
            proposal,
            decision.assignee_id,
            actor=actor,
            now=now,
            explanation=decision.explanation,
            evaluation_id=evaluation.id if evaluation else None,
            event_type=ProposalEventType.ASSIGNED,
        )
    else:
        logger.info(
            "assignment.no_candidate",
            proposal=str(proposal.id),
            team=str(proposal.team_id),
            fallback=decision.fallback,
        )
        session.add(
            ProposalEvent(
                proposal_id=proposal.id,
                actor_id=actor.user_id if actor else None,
                type=ProposalEventType.ESCALATED,
                payload={"reason": decision.explanation, "fallback": decision.fallback},
                rule_evaluation_id=evaluation.id if evaluation else None,
                created_at=now,
            )
        )
        await session.flush()

    return decision


async def _apply_manual(
    session: AsyncSession,
    proposal: Proposal,
    user_id: uuid.UUID,
    actor: Principal | None,
    reason: str | None,
    now: datetime,
) -> AssignmentDecision:
    """A manager overriding the engine. Allowed, but never silent."""
    policy = await get_active_policy(session, proposal.team_id)
    if policy is not None and not policy.allow_manual_override:
        raise ValidationError(
            "This team's assignment policy does not allow manual override."
        )

    member = await session.scalar(
        select(Membership).where(
            Membership.team_id == proposal.team_id, Membership.user_id == user_id
        )
    )
    if member is None:
        raise ValidationError("That person is not a member of this proposal's team.")

    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise NotFoundError("That user does not exist.")

    explanation = f"Manually assigned to {user.display_name}" + (
        f" — {reason}" if reason else ""
    )
    await _apply(
        session,
        proposal,
        user_id,
        actor=actor,
        now=now,
        explanation=explanation,
        evaluation_id=None,
        event_type=ProposalEventType.REASSIGNED
        if proposal.assigned_to is not None
        else ProposalEventType.ASSIGNED,
        reason=reason,
    )

    return AssignmentDecision(
        assignee_id=user_id, mode="manual", explanation=explanation
    )


async def _apply(
    session: AsyncSession,
    proposal: Proposal,
    assignee_id: uuid.UUID,
    *,
    actor: Principal | None,
    now: datetime,
    explanation: str,
    evaluation_id: uuid.UUID | None,
    event_type: ProposalEventType,
    reason: str | None = None,
) -> None:
    before = {"assigned_to": str(proposal.assigned_to) if proposal.assigned_to else None}

    proposal.previous_owner = proposal.assigned_to
    proposal.assigned_to = assignee_id
    proposal.assigned_at = now
    if proposal.status is ProposalStatus.NEW:
        proposal.status = ProposalStatus.ASSIGNED

    session.add(
        ProposalEvent(
            proposal_id=proposal.id,
            actor_id=actor.user_id if actor else None,
            type=event_type,
            payload={
                "assignee_id": str(assignee_id),
                "previous_owner": before["assigned_to"],
                "explanation": explanation,
                "reason": reason,
            },
            rule_evaluation_id=evaluation_id,
            created_at=now,
        )
    )
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.ASSIGN,
        entity_type="proposal",
        entity_id=proposal.id,
        actor=actor,
        team_id=proposal.team_id,
        before=before,
        after={"assigned_to": str(assignee_id), "explanation": explanation},
    )


def _assignment_facts(
    proposal: Proposal, member_count: int, now: datetime
) -> dict[str, Any]:
    days_to_bcd: float | None = None
    if proposal.bcd is not None:
        bcd = proposal.bcd if proposal.bcd.tzinfo else proposal.bcd.replace(tzinfo=UTC)
        days_to_bcd = (bcd - now).total_seconds() / 86400.0

    return {
        "proposal.title": proposal.title,
        "proposal.status": proposal.status.value,
        "proposal.value": float(proposal.estimated_value or 0),
        "proposal.days_to_bcd": days_to_bcd if days_to_bcd is not None else 9999.0,
        "proposal.customer": proposal.customer_name or "",
        "proposal.required_labels": frozenset(
            str(x) for x in (proposal.required_labels or ())
        ),
        "team.member_count": member_count,
    }


# ── policy authoring ───────────────────────────────────────────────────


async def publish_policy(
    session: AsyncSession,
    *,
    actor: Principal,
    team_id: uuid.UUID,
    name: str,
    eligibility: dict[str, Any],
    capacity: dict[str, Any],
    distribution: dict[str, Any],
    tie_break: str = "longest_idle",
    fallback: str = "notify_manager",
    allow_manual_override: bool = True,
    note: str | None = None,
) -> AssignmentPolicy:
    """Create or update a team's policy and make it live. Versioned, so it reverts."""
    policy = await session.scalar(
        select(AssignmentPolicy).where(
            AssignmentPolicy.team_id == team_id, AssignmentPolicy.name == name
        )
    )

    if policy is None:
        policy = AssignmentPolicy(team_id=team_id, name=name, version=0)
        session.add(policy)

    before = {
        "eligibility": policy.eligibility,
        "capacity": policy.capacity,
        "distribution": policy.distribution,
    }

    policy.eligibility = eligibility
    policy.capacity = capacity
    policy.distribution = distribution
    policy.tie_break = tie_break
    policy.fallback = fallback
    policy.allow_manual_override = allow_manual_override
    policy.version += 1
    policy.published_by = actor.user_id
    policy.published_at = datetime.now(UTC)

    # Exactly one active policy per team; two would make assignment non-deterministic.
    for other in (
        await session.scalars(
            select(AssignmentPolicy).where(
                AssignmentPolicy.team_id == team_id, AssignmentPolicy.id != policy.id
            )
        )
    ).all():
        other.active = False
    policy.active = True

    await session.flush()

    session.add(
        AssignmentPolicyVersion(
            policy_id=policy.id,
            version=policy.version,
            snapshot={
                "eligibility": eligibility,
                "capacity": capacity,
                "distribution": distribution,
                "tie_break": tie_break,
                "fallback": fallback,
                "allow_manual_override": allow_manual_override,
            },
            note=note,
            author_id=actor.user_id,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.PUBLISH,
        entity_type="assignment_policy",
        entity_id=policy.id,
        actor=actor,
        team_id=team_id,
        before=before,
        after={"version": policy.version, "note": note},
    )
    return policy
