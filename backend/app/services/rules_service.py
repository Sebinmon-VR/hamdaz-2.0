"""Rule set persistence, versioning and evaluation (§5.2).

The pure engine lives in :mod:`app.core.rules`. This is the part that talks to the database:
loading the right rule set, recording the evaluation, and handling publish and revert.

Simulation runs the identical code path as a real evaluation, with ``simulated=True`` on the
recorded row and no actions applied. Anything else would make the preview a lie.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_correlation_id, get_logger
from app.core.principal import Principal
from app.core.rules.evaluator import (
    CompiledRule,
    Decision,
    RuleValidationError,
    evaluate_rule_set,
    validate_rule,
)
from app.core.rules.registry import DecisionPoint, get_decision_point
from app.models.platform import AuditAction
from app.models.rules import Rule, RuleEvaluation, RuleSet, RuleSetVersion
from app.services import audit_service

logger = get_logger(__name__)


# ── selection ──────────────────────────────────────────────────────────


async def find_active_rule_set(
    session: AsyncSession, decision_point: str, team_id: uuid.UUID | None
) -> RuleSet | None:
    """The rule set that governs a decision for a team.

    Precedence: enabled and published, highest ``priority``, then **team-scoped before
    org-wide**. A team that has configured its own policy is stating an intent that the org
    default should not override.
    """
    get_decision_point(decision_point)

    query = (
        select(RuleSet)
        .where(RuleSet.decision_point == decision_point, RuleSet.enabled.is_(True))
        .options(selectinload(RuleSet.rules))
    )
    query = query.where(
        or_(RuleSet.team_id.is_(None), RuleSet.team_id == team_id)
        if team_id is not None
        else RuleSet.team_id.is_(None)
    )

    candidates = list((await session.scalars(query)).all())
    if not candidates:
        return None

    candidates.sort(
        key=lambda rs: (rs.priority, 1 if rs.team_id is not None else 0, rs.version),
        reverse=True,
    )
    return candidates[0]


def compile_rules(rule_set: RuleSet) -> list[CompiledRule]:
    """Flatten ORM rows into the evaluator's database-free input."""
    return [
        CompiledRule(
            id=str(rule.id),
            name=rule.name,
            position=rule.position,
            conditions=rule.conditions or {},
            actions=list(rule.actions or []),
            enabled=rule.enabled,
        )
        for rule in rule_set.rules
    ]


# ── evaluation ─────────────────────────────────────────────────────────


async def evaluate(
    session: AsyncSession,
    *,
    decision_point: str,
    facts: Mapping[str, Any],
    team_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor: Principal | None = None,
    simulated: bool = False,
    rule_set: RuleSet | None = None,
) -> tuple[Decision, RuleEvaluation | None]:
    """Evaluate a decision point and record why.

    Returns ``(decision, evaluation_row)``. The row is what the developer panel's rule
    inspector renders — the facts the engine saw, which rules matched, and the outcome.
    """
    dp = get_decision_point(decision_point)
    started = time.perf_counter()

    active = rule_set or await find_active_rule_set(session, decision_point, team_id)

    if active is None:
        # No configured policy is a legitimate state, not an error: the caller falls back to
        # its own default. It is still recorded, so "why did nothing happen?" is answerable.
        decision = Decision(
            decision_point=decision_point,
            matched_rule_ids=(),
            actions=(),
            trace=(),
            facts=dict(facts),
        )
    else:
        decision = evaluate_rule_set(
            decision_point=dp,
            rules=compile_rules(active),
            facts=facts,
            evaluate_all=active.evaluate_all,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)

    evaluation = RuleEvaluation(
        decision_point=decision_point,
        rule_set_id=active.id if active else None,
        rule_set_version=active.version if active else None,
        team_id=team_id,
        entity_type=entity_type or dp.entity_type,
        entity_id=str(entity_id) if entity_id else None,
        facts=audit_service._jsonable(dict(facts)),
        matched_rule_ids=list(decision.matched_rule_ids),
        trace=decision.to_trace_json(),
        outcome={"actions": [dict(a) for a in decision.actions]},
        simulated=simulated,
        actor_id=actor.user_id if actor else None,
        duration_ms=duration_ms,
        correlation_id=get_correlation_id(),
        created_at=datetime.now(UTC),
    )
    session.add(evaluation)
    await session.flush()

    return decision, evaluation


# ── authoring ──────────────────────────────────────────────────────────


async def get_rule_set(session: AsyncSession, rule_set_id: uuid.UUID) -> RuleSet:
    rule_set = await session.scalar(
        select(RuleSet).where(RuleSet.id == rule_set_id).options(selectinload(RuleSet.rules))
    )
    if rule_set is None:
        raise NotFoundError("That rule set does not exist.")
    return rule_set


async def list_rule_sets(
    session: AsyncSession,
    *,
    decision_point: str | None = None,
    team_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RuleSet], int]:
    query = select(RuleSet)
    if decision_point:
        query = query.where(RuleSet.decision_point == decision_point)
    if team_id is not None:
        query = query.where(or_(RuleSet.team_id == team_id, RuleSet.team_id.is_(None)))

    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await session.scalars(
            query.options(selectinload(RuleSet.rules))
            .order_by(RuleSet.decision_point, RuleSet.priority.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


async def create_rule_set(
    session: AsyncSession,
    *,
    actor: Principal,
    decision_point: str,
    name: str,
    team_id: uuid.UUID | None = None,
    description: str | None = None,
    priority: int = 0,
    evaluate_all: bool = False,
) -> RuleSet:
    get_decision_point(decision_point)

    if await session.scalar(
        select(RuleSet).where(
            RuleSet.decision_point == decision_point,
            RuleSet.team_id.is_(team_id),
            RuleSet.name == name,
        )
    ):
        raise ConflictError(f"A rule set named {name!r} already exists for this decision point.")

    rule_set = RuleSet(
        decision_point=decision_point,
        team_id=team_id,
        name=name.strip(),
        description=description,
        priority=priority,
        evaluate_all=evaluate_all,
        # New sets start disabled. An admin publishes deliberately, after simulating.
        enabled=False,
        version=0,
    )
    session.add(rule_set)
    await session.flush()

    # A freshly added object has its collections unloaded, so the caller reading
    # rule_set.rules would emit a lazy SELECT and raise MissingGreenlet. Loading it
    # explicitly here costs one trivial query and keeps the returned object safe to render.
    await session.refresh(rule_set, ["rules"])

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="rule_set",
        entity_id=rule_set.id,
        actor=actor,
        team_id=team_id,
        after={"decision_point": decision_point, "name": rule_set.name},
    )
    return rule_set


async def replace_rules(
    session: AsyncSession,
    *,
    actor: Principal,
    rule_set_id: uuid.UUID,
    rules: Sequence[Mapping[str, Any]],
) -> RuleSet:
    """Replace a rule set's rules wholesale, validating every one first.

    Validation happens before anything is written, so a rule set is never left half-updated
    by a rejected rule in the middle of the list.
    """
    rule_set = await get_rule_set(session, rule_set_id)
    dp = get_decision_point(rule_set.decision_point)

    prepared: list[dict[str, Any]] = []
    for index, spec in enumerate(rules):
        conditions = spec.get("conditions") or {}
        actions = spec.get("actions") or []
        try:
            validate_rule(conditions, actions, dp)
        except RuleValidationError as exc:
            name = spec.get("name") or f"rule {index + 1}"
            raise ValidationError(f"{name}: {exc}") from exc

        prepared.append(
            {
                "position": int(spec.get("position", index)),
                "name": str(spec.get("name") or f"Rule {index + 1}"),
                "conditions": conditions,
                "actions": actions,
                "enabled": bool(spec.get("enabled", True)),
            }
        )

    positions = [p["position"] for p in prepared]
    if len(set(positions)) != len(positions):
        raise ValidationError("Two rules share the same position. Positions must be unique.")

    before = _snapshot(rule_set)

    for existing in list(rule_set.rules):
        await session.delete(existing)
    await session.flush()

    for spec in prepared:
        session.add(Rule(rule_set_id=rule_set.id, **spec))
    await session.flush()
    await session.refresh(rule_set, ["rules"])

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        entity_type="rule_set",
        entity_id=rule_set.id,
        actor=actor,
        team_id=rule_set.team_id,
        before={"rule_count": len(before.get("rules", []))},
        after={"rule_count": len(prepared)},
    )
    return rule_set


async def publish(
    session: AsyncSession,
    *,
    actor: Principal,
    rule_set_id: uuid.UUID,
    note: str | None = None,
    enable: bool = True,
) -> RuleSetVersion:
    """Snapshot and activate a rule set. Every publish is revertible."""
    rule_set = await get_rule_set(session, rule_set_id)

    if not rule_set.rules:
        raise ValidationError("This rule set has no rules. Add at least one before publishing.")

    rule_set.version += 1
    rule_set.enabled = enable
    rule_set.published_by = actor.user_id
    rule_set.published_at = datetime.now(UTC)

    version = RuleSetVersion(
        rule_set_id=rule_set.id,
        version=rule_set.version,
        snapshot=_snapshot(rule_set),
        note=note,
        author_id=actor.user_id,
        created_at=datetime.now(UTC),
    )
    session.add(version)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.PUBLISH,
        entity_type="rule_set",
        entity_id=rule_set.id,
        actor=actor,
        team_id=rule_set.team_id,
        after={"version": rule_set.version, "enabled": enable, "note": note},
    )
    logger.info(
        "rules.published",
        rule_set=str(rule_set.id),
        decision_point=rule_set.decision_point,
        version=rule_set.version,
    )
    return version


async def revert(
    session: AsyncSession, *, actor: Principal, rule_set_id: uuid.UUID, version: int
) -> RuleSet:
    """Restore a previous version, as a new version.

    Reverting forward rather than rewinding keeps the version history honest: you can see
    that a revert happened, and revert the revert.
    """
    rule_set = await get_rule_set(session, rule_set_id)

    snapshot_row = await session.scalar(
        select(RuleSetVersion).where(
            RuleSetVersion.rule_set_id == rule_set_id, RuleSetVersion.version == version
        )
    )
    if snapshot_row is None:
        raise NotFoundError(f"There is no version {version} of this rule set.")

    payload = snapshot_row.snapshot
    for existing in list(rule_set.rules):
        await session.delete(existing)
    await session.flush()

    for spec in payload.get("rules", []):
        session.add(
            Rule(
                rule_set_id=rule_set.id,
                position=spec["position"],
                name=spec["name"],
                conditions=spec["conditions"],
                actions=spec["actions"],
                enabled=spec.get("enabled", True),
            )
        )

    rule_set.evaluate_all = payload.get("evaluate_all", rule_set.evaluate_all)
    rule_set.priority = payload.get("priority", rule_set.priority)
    await session.flush()
    await session.refresh(rule_set, ["rules"])

    await publish(session, actor=actor, rule_set_id=rule_set.id, note=f"Reverted to v{version}")
    return rule_set


async def list_versions(
    session: AsyncSession, rule_set_id: uuid.UUID
) -> list[RuleSetVersion]:
    return list(
        (
            await session.scalars(
                select(RuleSetVersion)
                .where(RuleSetVersion.rule_set_id == rule_set_id)
                .order_by(RuleSetVersion.version.desc())
            )
        ).all()
    )


def _snapshot(rule_set: RuleSet) -> dict[str, Any]:
    return {
        "decision_point": rule_set.decision_point,
        "name": rule_set.name,
        "priority": rule_set.priority,
        "evaluate_all": rule_set.evaluate_all,
        "rules": [
            {
                "position": r.position,
                "name": r.name,
                "conditions": r.conditions,
                "actions": r.actions,
                "enabled": r.enabled,
            }
            for r in sorted(rule_set.rules, key=lambda r: r.position)
        ],
    }


# ── simulation ─────────────────────────────────────────────────────────


async def simulate(
    session: AsyncSession,
    *,
    actor: Principal,
    rule_set_id: uuid.UUID,
    fact_sets: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Dry-run a rule set — including an unpublished one — against sample facts.

    This is the "simulate before publish" requirement. It runs the same evaluator the live
    path uses, so what the admin sees here is exactly what will happen.
    """
    rule_set = await get_rule_set(session, rule_set_id)
    dp: DecisionPoint = get_decision_point(rule_set.decision_point)

    results: list[dict[str, Any]] = []
    for facts in fact_sets:
        unknown = set(facts) - set(dp.fact_map)
        decision, _ = await evaluate(
            session,
            decision_point=rule_set.decision_point,
            facts=facts,
            team_id=rule_set.team_id,
            actor=actor,
            simulated=True,
            rule_set=rule_set,
        )
        results.append(
            {
                "facts": dict(facts),
                "matched": decision.matched,
                "matched_rule_ids": list(decision.matched_rule_ids),
                "matched_rule_names": [
                    r.name for r in decision.trace if r.rule_id in decision.matched_rule_ids
                ],
                "actions": [dict(a) for a in decision.actions],
                "trace": decision.to_trace_json(),
                # Surfaced rather than silently ignored: a typo'd fact key is the most common
                # reason a rule "does not work" and the hardest to spot.
                "unrecognised_facts": sorted(unknown),
            }
        )

    return results
