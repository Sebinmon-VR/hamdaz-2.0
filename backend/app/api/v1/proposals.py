"""Proposals API (§9 Phase 4)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentPrincipal, DbDep, require
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.principal import Principal
from app.core.rbac import Scope
from app.models.platform import AuditAction
from app.models.proposals import (
    OPEN_STATUS_VALUES,
    Proposal,
    ProposalEvent,
    ProposalEventType,
    ProposalSource,
    ProposalStatus,
)
from app.schemas.common import Message, Page, Pagination, pagination
from app.services import assignment_service, audit_service

router = APIRouter(prefix="/proposals", tags=["proposals"])

PaginationDep = Annotated[Pagination, Depends(pagination)]


class ProposalOut(BaseModel):
    id: str
    team_id: str
    external_ref: str | None = None
    title: str
    customer_name: str | None = None
    status: str
    assigned_to: str | None = None
    bcd: str | None = None
    estimated_value: float | None = None
    currency: str | None = None
    priority_score: int
    required_labels: list[str] = Field(default_factory=list)
    source: str
    created_at: str


class ProposalCreate(BaseModel):
    team_id: uuid.UUID
    title: str = Field(min_length=2)
    description: str | None = None
    customer_name: str | None = None
    external_ref: str | None = None
    bcd: datetime | None = None
    estimated_value: float | None = None
    currency: str | None = Field(default=None, max_length=3)
    required_labels: list[str] = Field(default_factory=list)
    #: Run the team's assignment policy immediately. Off by default so a bulk import does
    #: not fire hundreds of assignments as a side effect of being created.
    auto_assign: bool = False


class AssignIn(BaseModel):
    #: Omit to let the policy choose; supply to override it (recorded either way).
    user_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=500)


class StatusIn(BaseModel):
    status: ProposalStatus
    note: str | None = None


def _out(p: Proposal) -> ProposalOut:
    return ProposalOut(
        id=str(p.id),
        team_id=str(p.team_id),
        external_ref=p.external_ref,
        title=p.title,
        customer_name=p.customer_name,
        status=p.status.value,
        assigned_to=str(p.assigned_to) if p.assigned_to else None,
        bcd=p.bcd.isoformat() if p.bcd else None,
        estimated_value=float(p.estimated_value) if p.estimated_value is not None else None,
        currency=p.currency,
        priority_score=p.priority_score,
        required_labels=[str(x) for x in (p.required_labels or [])],
        source=p.source.value,
        created_at=p.created_at.isoformat(),
    )


async def _load(session: AsyncSession, proposal_id: uuid.UUID) -> Proposal:
    proposal: Proposal | None = await session.scalar(
        select(Proposal).where(Proposal.id == proposal_id)
    )
    if proposal is None:
        raise NotFoundError("That proposal does not exist.")
    return proposal


def _assert_visible(principal: Principal, proposal: Proposal) -> None:
    """Team isolation, enforced on single-object reads.

    List endpoints filter by ``teams_with``; a fetch-by-id has no filter to rely on, so the
    check has to be explicit. This is exactly where multi-tenant systems leak.
    """
    if principal.has("proposals.read", Scope.TEAM, team_id=proposal.team_id):
        return
    if proposal.assigned_to == principal.user_id and principal.has(
        "proposals.read", Scope.OWN, team_id=proposal.team_id
    ):
        return
    raise PermissionDeniedError(
        "You do not have access to this proposal.", permission="proposals.read"
    )


@router.get("/", response_model=Page[ProposalOut])
async def list_proposals(
    session: DbDep,
    principal: CurrentPrincipal,
    page: PaginationDep,
    team_id: Annotated[uuid.UUID | None, Query()] = None,
    proposal_status: Annotated[ProposalStatus | None, Query(alias="status")] = None,
    assigned_to: Annotated[uuid.UUID | None, Query()] = None,
    open_only: Annotated[bool, Query()] = False,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> Page[ProposalOut]:
    """Proposals the caller may see.

    Scoping is applied to the query, not after it: a caller can only ever be shown rows from
    teams where they hold ``proposals.read``.
    """
    visible = principal.teams_with("proposals.read", Scope.TEAM)

    query = select(Proposal)
    if principal.is_super_admin:
        pass
    elif visible:
        query = query.where(Proposal.team_id.in_(visible))
    else:
        # No team-wide read anywhere: fall back to their own assignments.
        query = query.where(Proposal.assigned_to == principal.user_id)

    if team_id is not None:
        if not principal.is_super_admin and team_id not in visible:
            raise PermissionDeniedError(
                "You do not have access to that team's proposals.",
                permission="proposals.read",
            )
        query = query.where(Proposal.team_id == team_id)

    if proposal_status is not None:
        query = query.where(Proposal.status == proposal_status)
    if open_only:
        query = query.where(Proposal.status.in_(OPEN_STATUS_VALUES))
    if assigned_to is not None:
        query = query.where(Proposal.assigned_to == assigned_to)
    if search:
        pattern = f"%{search.lower()}%"
        query = query.where(
            func.lower(Proposal.title).like(pattern)
            | func.lower(func.coalesce(Proposal.customer_name, "")).like(pattern)
            | func.lower(func.coalesce(Proposal.external_ref, "")).like(pattern)
        )

    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (
        await session.scalars(
            query.order_by(Proposal.bcd.asc().nullslast(), desc(Proposal.created_at))
            .limit(page.limit)
            .offset(page.offset)
        )
    ).all()

    return Page.of([_out(p) for p in rows], total=total, limit=page.limit, offset=page.offset)


@router.post(
    "/",
    response_model=ProposalOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require("proposals.create", Scope.TEAM))],
)
async def create_proposal(
    body: ProposalCreate, session: DbDep, principal: CurrentPrincipal
) -> ProposalOut:
    if not principal.has("proposals.create", Scope.TEAM, team_id=body.team_id):
        raise PermissionDeniedError(
            "You cannot create proposals in that team.", permission="proposals.create"
        )

    proposal = Proposal(
        team_id=body.team_id,
        title=body.title,
        description=body.description,
        customer_name=body.customer_name,
        external_ref=body.external_ref,
        bcd=body.bcd,
        estimated_value=body.estimated_value,
        currency=body.currency,
        required_labels=body.required_labels,
        source=ProposalSource.MANUAL,
        status=ProposalStatus.NEW,
    )
    session.add(proposal)
    await session.flush()

    session.add(
        ProposalEvent(
            proposal_id=proposal.id,
            actor_id=principal.user_id,
            type=ProposalEventType.CREATED,
            created_at=datetime.now(UTC),
        )
    )
    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="proposal",
        entity_id=proposal.id,
        actor=principal,
        team_id=proposal.team_id,
        after={"title": proposal.title, "customer": proposal.customer_name},
    )

    if body.auto_assign:
        await assignment_service.assign_proposal(session, proposal=proposal, actor=principal)

    return _out(proposal)


@router.get("/{proposal_id}", response_model=ProposalOut)
async def get_proposal(
    proposal_id: uuid.UUID, session: DbDep, principal: CurrentPrincipal
) -> ProposalOut:
    proposal = await _load(session, proposal_id)
    _assert_visible(principal, proposal)
    return _out(proposal)


class EventOut(BaseModel):
    id: str
    type: str
    actor_id: str | None = None
    payload: dict[str, Any] | None = None
    rule_evaluation_id: str | None = None
    created_at: str


@router.get("/{proposal_id}/timeline", response_model=list[EventOut])
async def timeline(
    proposal_id: uuid.UUID, session: DbDep, principal: CurrentPrincipal
) -> list[EventOut]:
    """The proposal's history.

    ``rule_evaluation_id`` links each automatic action back to the decision that caused it,
    so the timeline explains itself rather than just listing what happened.
    """
    proposal = await _load(session, proposal_id)
    _assert_visible(principal, proposal)

    rows = (
        await session.scalars(
            select(ProposalEvent)
            .where(ProposalEvent.proposal_id == proposal_id)
            .order_by(ProposalEvent.created_at)
        )
    ).all()

    return [
        EventOut(
            id=str(e.id),
            type=e.type.value,
            actor_id=str(e.actor_id) if e.actor_id else None,
            payload=e.payload,
            rule_evaluation_id=str(e.rule_evaluation_id) if e.rule_evaluation_id else None,
            created_at=e.created_at.isoformat(),
        )
        for e in rows
    ]


@router.post("/{proposal_id}/assign")
async def assign(
    proposal_id: uuid.UUID,
    body: AssignIn,
    session: DbDep,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    """Assign a proposal.

    With no ``user_id`` the team's policy chooses and the reasoning is recorded. With one, a
    manager is overriding the engine — allowed, but never silent.
    """
    proposal = await _load(session, proposal_id)

    needed = "proposals.reassign" if proposal.assigned_to is not None else "proposals.assign"
    if not principal.has(needed, Scope.TEAM, team_id=proposal.team_id):
        raise PermissionDeniedError(
            "You do not have permission to assign proposals in this team.", permission=needed
        )

    decision = await assignment_service.assign_proposal(
        session,
        proposal=proposal,
        actor=principal,
        force_user_id=body.user_id,
        reason=body.reason,
    )

    return {
        "assigned": decision.assigned,
        "assignee_id": str(decision.assignee_id) if decision.assignee_id else None,
        "explanation": decision.explanation,
        "fallback": decision.fallback,
        "ranked": decision.to_json()["ranked"],
        "excluded": decision.to_json()["excluded"],
    }


@router.post("/{proposal_id}/status", response_model=ProposalOut)
async def change_status(
    proposal_id: uuid.UUID,
    body: StatusIn,
    session: DbDep,
    principal: CurrentPrincipal,
) -> ProposalOut:
    proposal = await _load(session, proposal_id)

    is_owner = proposal.assigned_to == principal.user_id
    scope = Scope.OWN if is_owner else Scope.TEAM
    if not principal.has("proposals.update", scope, team_id=proposal.team_id):
        raise PermissionDeniedError(
            "You do not have permission to change this proposal.",
            permission="proposals.update",
        )

    before = proposal.status.value
    proposal.status = body.status

    session.add(
        ProposalEvent(
            proposal_id=proposal.id,
            actor_id=principal.user_id,
            type=ProposalEventType.STATUS_CHANGED,
            payload={"from": before, "to": body.status.value, "note": body.note},
            created_at=datetime.now(UTC),
        )
    )
    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        entity_type="proposal",
        entity_id=proposal.id,
        actor=principal,
        team_id=proposal.team_id,
        before={"status": before},
        after={"status": body.status.value},
    )
    await session.flush()
    return _out(proposal)


class WorkloadRow(BaseModel):
    user_id: str
    display_name: str
    labels: list[str]
    open_task_count: int
    capacity: float
    effective_load: float
    on_leave: bool


@router.get(
    "/workload/{team_id}",
    response_model=list[WorkloadRow],
    dependencies=[Depends(require("reports.read_team", Scope.TEAM))],
)
async def workload(team_id: uuid.UUID, session: DbDep) -> list[WorkloadRow]:
    """Current load per member, as the assignment engine sees it.

    Effective load, not raw count: that is the number that explains why a new joiner with two
    proposals is being passed over.
    """
    from app.core.rules.assignment import capacity_for, effective_load

    config = assignment_service.policy_config(
        await assignment_service.get_active_policy(session, team_id)
    )
    candidates = await assignment_service.build_candidates(session, team_id)

    rows: list[WorkloadRow] = []
    for c in candidates:
        capacity = capacity_for(c, config["capacity"])
        rows.append(
            WorkloadRow(
                user_id=str(c.user_id),
                display_name=c.display_name,
                labels=sorted(c.labels),
                open_task_count=c.open_task_count,
                capacity=capacity,
                effective_load=round(effective_load(c, capacity), 2),
                on_leave=c.on_leave,
            )
        )

    rows.sort(key=lambda r: r.effective_load, reverse=True)
    return rows


@router.delete(
    "/{proposal_id}",
    response_model=Message,
    dependencies=[Depends(require("proposals.delete", Scope.TEAM))],
)
async def delete_proposal(
    proposal_id: uuid.UUID, session: DbDep, principal: CurrentPrincipal
) -> Message:
    proposal = await _load(session, proposal_id)
    if not principal.has("proposals.delete", Scope.TEAM, team_id=proposal.team_id):
        raise PermissionDeniedError(
            "You cannot delete proposals in this team.", permission="proposals.delete"
        )

    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        entity_type="proposal",
        entity_id=proposal.id,
        actor=principal,
        team_id=proposal.team_id,
        before={"title": proposal.title, "status": proposal.status.value},
    )
    await session.delete(proposal)
    await session.flush()
    return Message(message="Proposal deleted.")
