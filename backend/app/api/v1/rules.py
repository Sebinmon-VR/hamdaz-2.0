"""Rules engine API (§5.2) — the admin panel's rule builder and the assignment policy.

Note the shape of every mutating endpoint: author freely, **simulate**, then publish. A rule
set is created disabled and stays that way until someone deliberately publishes it.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentPrincipal, DbDep, require
from app.core.rbac import Scope
from app.core.rules.registry import DECISION_POINTS, get_decision_point
from app.schemas.common import Message, Page, Pagination, pagination
from app.services import assignment_service, rules_service

router = APIRouter(prefix="/rules", tags=["rules"])

PaginationDep = Annotated[Pagination, Depends(pagination)]


# ── the registry, served to the rule builder ───────────────────────────


class FactOut(BaseModel):
    key: str
    type: str
    description: str
    operators: list[str]
    choices: list[str] = Field(default_factory=list)


class ActionParamOut(BaseModel):
    key: str
    type: str
    description: str
    required: bool
    choices: list[str] = Field(default_factory=list)


class ActionOut(BaseModel):
    type: str
    description: str
    params: list[ActionParamOut] = Field(default_factory=list)


class DecisionPointOut(BaseModel):
    key: str
    name: str
    description: str
    fires_when: str
    entity_type: str
    facts: list[FactOut]
    actions: list[ActionOut]


@router.get("/decision-points", response_model=list[DecisionPointOut])
async def list_decision_points() -> list[DecisionPointOut]:
    """Every decision point, with its facts and actions.

    The rule builder renders entirely from this, so it can never offer a condition or action
    the engine does not support.
    """
    return [
        DecisionPointOut(
            key=dp.key,
            name=dp.name,
            description=dp.description,
            fires_when=dp.fires_when,
            entity_type=dp.entity_type,
            facts=[
                FactOut(
                    key=f.key,
                    type=f.type.value,
                    description=f.description,
                    operators=[o.value for o in f.operators],
                    choices=list(f.choices),
                )
                for f in dp.facts
            ],
            actions=[
                ActionOut(
                    type=a.type,
                    description=a.description,
                    params=[
                        ActionParamOut(
                            key=p.key,
                            type=p.type.value,
                            description=p.description,
                            required=p.required,
                            choices=list(p.choices),
                        )
                        for p in a.params
                    ],
                )
                for a in dp.actions
            ],
        )
        for dp in DECISION_POINTS
    ]


# ── rule sets ──────────────────────────────────────────────────────────


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    position: int = Field(ge=0)
    conditions: dict[str, Any]
    actions: list[dict[str, Any]]
    enabled: bool = True


class RuleOut(RuleIn):
    id: str


class RuleSetOut(BaseModel):
    id: str
    decision_point: str
    team_id: str | None = None
    name: str
    description: str | None = None
    enabled: bool
    priority: int
    evaluate_all: bool
    version: int
    published_at: str | None = None
    rules: list[RuleOut] = Field(default_factory=list)


class RuleSetCreate(BaseModel):
    decision_point: str
    name: str = Field(min_length=2, max_length=160)
    team_id: uuid.UUID | None = None
    description: str | None = None
    priority: int = 0
    evaluate_all: bool = False


class RulesReplace(BaseModel):
    rules: list[RuleIn]


class PublishIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    enable: bool = True


class SimulateIn(BaseModel):
    fact_sets: list[dict[str, Any]] = Field(min_length=1, max_length=50)


def _rule_set_out(rule_set: Any) -> RuleSetOut:
    return RuleSetOut(
        id=str(rule_set.id),
        decision_point=rule_set.decision_point,
        team_id=str(rule_set.team_id) if rule_set.team_id else None,
        name=rule_set.name,
        description=rule_set.description,
        enabled=rule_set.enabled,
        priority=rule_set.priority,
        evaluate_all=rule_set.evaluate_all,
        version=rule_set.version,
        published_at=rule_set.published_at.isoformat() if rule_set.published_at else None,
        rules=[
            RuleOut(
                id=str(r.id),
                name=r.name,
                position=r.position,
                conditions=r.conditions or {},
                actions=list(r.actions or []),
                enabled=r.enabled,
            )
            for r in sorted(rule_set.rules, key=lambda r: r.position)
        ],
    )


@router.get(
    "/sets",
    response_model=Page[RuleSetOut],
    dependencies=[Depends(require("rules.read", Scope.TEAM))],
)
async def list_rule_sets(
    session: DbDep,
    page: PaginationDep,
    decision_point: Annotated[str | None, Query()] = None,
    team_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[RuleSetOut]:
    sets, total = await rules_service.list_rule_sets(
        session,
        decision_point=decision_point,
        team_id=team_id,
        limit=page.limit,
        offset=page.offset,
    )
    return Page.of(
        [_rule_set_out(rs) for rs in sets], total=total, limit=page.limit, offset=page.offset
    )


@router.post(
    "/sets",
    response_model=RuleSetOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require("rules.edit_team", Scope.TEAM))],
)
async def create_rule_set(
    body: RuleSetCreate, session: DbDep, principal: CurrentPrincipal
) -> RuleSetOut:
    get_decision_point(body.decision_point)  # 400 rather than a 500 later

    # An org-wide rule set affects every team, so it needs the wider permission.
    if body.team_id is None and not principal.has("rules.edit_org", Scope.ALL):
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError(
            "Creating an organisation-wide rule set requires rules.edit_org.",
            permission="rules.edit_org",
            scope="all",
        )

    rule_set = await rules_service.create_rule_set(
        session,
        actor=principal,
        decision_point=body.decision_point,
        name=body.name,
        team_id=body.team_id,
        description=body.description,
        priority=body.priority,
        evaluate_all=body.evaluate_all,
    )
    return _rule_set_out(rule_set)


@router.get(
    "/sets/{rule_set_id}",
    response_model=RuleSetOut,
    dependencies=[Depends(require("rules.read", Scope.TEAM))],
)
async def get_rule_set(rule_set_id: uuid.UUID, session: DbDep) -> RuleSetOut:
    return _rule_set_out(await rules_service.get_rule_set(session, rule_set_id))


@router.put(
    "/sets/{rule_set_id}/rules",
    response_model=RuleSetOut,
    dependencies=[Depends(require("rules.edit_team", Scope.TEAM))],
)
async def replace_rules(
    rule_set_id: uuid.UUID,
    body: RulesReplace,
    session: DbDep,
    principal: CurrentPrincipal,
) -> RuleSetOut:
    """Replace a set's rules. Every rule is validated before anything is written."""
    rule_set = await rules_service.replace_rules(
        session,
        actor=principal,
        rule_set_id=rule_set_id,
        rules=[r.model_dump() for r in body.rules],
    )
    return _rule_set_out(rule_set)


@router.post(
    "/sets/{rule_set_id}/simulate",
    dependencies=[Depends(require("rules.simulate", Scope.TEAM))],
)
async def simulate(
    rule_set_id: uuid.UUID,
    body: SimulateIn,
    session: DbDep,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    """Dry-run a rule set — including an unpublished one — against sample facts.

    Runs the same evaluator the live path uses, so what you see here is what will happen.
    """
    results = await rules_service.simulate(
        session, actor=principal, rule_set_id=rule_set_id, fact_sets=body.fact_sets
    )
    return {"results": results, "simulated": True}


@router.post(
    "/sets/{rule_set_id}/publish",
    response_model=Message,
    dependencies=[Depends(require("rules.publish", Scope.TEAM))],
)
async def publish(
    rule_set_id: uuid.UUID,
    body: PublishIn,
    session: DbDep,
    principal: CurrentPrincipal,
) -> Message:
    version = await rules_service.publish(
        session,
        actor=principal,
        rule_set_id=rule_set_id,
        note=body.note,
        enable=body.enable,
    )
    return Message(message=f"Published version {version.version}.")


class VersionOut(BaseModel):
    version: int
    note: str | None = None
    author_id: str | None = None
    created_at: str
    rule_count: int


@router.get(
    "/sets/{rule_set_id}/versions",
    response_model=list[VersionOut],
    dependencies=[Depends(require("rules.read", Scope.TEAM))],
)
async def list_versions(rule_set_id: uuid.UUID, session: DbDep) -> list[VersionOut]:
    versions = await rules_service.list_versions(session, rule_set_id)
    return [
        VersionOut(
            version=v.version,
            note=v.note,
            author_id=str(v.author_id) if v.author_id else None,
            created_at=v.created_at.isoformat(),
            rule_count=len(v.snapshot.get("rules", [])),
        )
        for v in versions
    ]


@router.post(
    "/sets/{rule_set_id}/revert/{version}",
    response_model=Message,
    dependencies=[Depends(require("rules.publish", Scope.TEAM))],
)
async def revert(
    rule_set_id: uuid.UUID, version: int, session: DbDep, principal: CurrentPrincipal
) -> Message:
    """Restore a previous version — as a new version, so the history stays honest."""
    rule_set = await rules_service.revert(
        session, actor=principal, rule_set_id=rule_set_id, version=version
    )
    return Message(
        message=f"Reverted to version {version}. Now live as version {rule_set.version}."
    )


# ── assignment policy (§5.4) ───────────────────────────────────────────


class AssignmentPolicyIn(BaseModel):
    name: str = Field(default="default", max_length=160)
    eligibility: dict[str, Any]
    capacity: dict[str, Any]
    distribution: dict[str, Any]
    tie_break: str = "longest_idle"
    fallback: str = "notify_manager"
    allow_manual_override: bool = True
    note: str | None = None


class AssignmentPolicyOut(BaseModel):
    id: str | None = None
    team_id: str
    name: str
    version: int
    active: bool
    eligibility: dict[str, Any]
    capacity: dict[str, Any]
    distribution: dict[str, Any]
    tie_break: str
    fallback: str
    allow_manual_override: bool
    is_default: bool = False


@router.get(
    "/assignment/{team_id}",
    response_model=AssignmentPolicyOut,
    dependencies=[Depends(require("rules.read", Scope.TEAM))],
)
async def get_assignment_policy(team_id: uuid.UUID, session: DbDep) -> AssignmentPolicyOut:
    """A team's live policy, or the shipped default when it has never configured one."""
    policy = await assignment_service.get_active_policy(session, team_id)
    config = assignment_service.policy_config(policy)

    return AssignmentPolicyOut(
        id=str(policy.id) if policy else None,
        team_id=str(team_id),
        name=policy.name if policy else "Built-in default",
        version=policy.version if policy else 0,
        active=policy.active if policy else False,
        eligibility=config["eligibility"],
        capacity=config["capacity"],
        distribution=config["distribution"],
        tie_break=config.get("tie_break", "longest_idle"),
        fallback=config.get("fallback", "notify_manager"),
        allow_manual_override=policy.allow_manual_override if policy else True,
        is_default=policy is None,
    )


@router.post(
    "/assignment/{team_id}/preview",
    dependencies=[Depends(require("rules.simulate", Scope.TEAM))],
)
async def preview_assignment(
    team_id: uuid.UUID,
    session: DbDep,
    body: AssignmentPolicyIn | None = None,
    count: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Who would get the next N proposals.

    Pass a policy body to preview an unsaved draft, or omit it to preview what is live. An
    assignment policy nobody can predict is one nobody will trust.
    """
    override = (
        {
            "eligibility": body.eligibility,
            "capacity": body.capacity,
            "distribution": body.distribution,
            "tie_break": body.tie_break,
            "fallback": body.fallback,
        }
        if body is not None
        else None
    )
    return await assignment_service.preview(
        session, team_id=team_id, count=count, policy_override=override
    )


@router.post(
    "/assignment/{team_id}/publish",
    response_model=Message,
    dependencies=[Depends(require("rules.publish", Scope.TEAM))],
)
async def publish_assignment_policy(
    team_id: uuid.UUID,
    body: AssignmentPolicyIn,
    session: DbDep,
    principal: CurrentPrincipal,
) -> Message:
    policy = await assignment_service.publish_policy(
        session,
        actor=principal,
        team_id=team_id,
        name=body.name,
        eligibility=body.eligibility,
        capacity=body.capacity,
        distribution=body.distribution,
        tie_break=body.tie_break,
        fallback=body.fallback,
        allow_manual_override=body.allow_manual_override,
        note=body.note,
    )
    return Message(
        message=f"Assignment policy {policy.name!r} is live as version {policy.version}."
    )


@router.get(
    "/assignment/{team_id}/candidates",
    dependencies=[Depends(require("rules.read", Scope.TEAM))],
)
async def list_candidates(team_id: uuid.UUID, session: DbDep) -> dict[str, Any]:
    """The team's members with the load and label data the engine actually sees."""
    candidates = await assignment_service.build_candidates(session, team_id)
    config = assignment_service.policy_config(
        await assignment_service.get_active_policy(session, team_id)
    )
    from app.core.rules.assignment import capacity_for, effective_load

    return {
        "candidates": [
            {
                "user_id": str(c.user_id),
                "display_name": c.display_name,
                "labels": sorted(c.labels),
                "open_task_count": c.open_task_count,
                "capacity": capacity_for(c, config["capacity"]),
                "effective_load": round(
                    effective_load(c, capacity_for(c, config["capacity"])), 2
                ),
                "on_leave": c.on_leave,
                "recent_assignments": c.recent_assignment_count,
            }
            for c in candidates
        ]
    }
