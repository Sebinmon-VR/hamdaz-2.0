"""The assignment policy: how work should be shared out.

**Nothing here assigns anything.** These endpoints edit the settings and show
what they would mean for real people. The scoring that acts on them is the next
piece of work, deliberately separate so the ratios can be set up, previewed and
argued about before any of it starts moving work around. Nothing here writes to
SharePoint either.

Reading is open to any signed-in user: the rule deciding how much work somebody
gets should be visible to the person it applies to.

Editing follows the reach rule in ``service.reach`` — super admins and the CEO
anywhere, a manager only on teams they belong to.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assignment import service
from app.assignment.schemas import EffectOut, PolicyIn, PolicyOut, PolicyPreviewOut
from app.assignment.service import PolicyError, PolicyNotFoundError
from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.labels import service as labels_service
from app.models.assignment import AssignmentPolicy
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.roles.deps import CurrentRoles
from app.teams import service as teams_service
from app.teams.service import TeamError

router = APIRouter(prefix="/assignment", tags=["assignment policy"])

Session = Annotated[AsyncSession, Depends(get_session)]


async def _team(session: AsyncSession, ref: str | None) -> Team | None:
    """Resolve a team from a slug or an id.

    Both appear in URLs everywhere else in this app, and a UI has no reason to
    know which one this module happens to want — asking for a UUID here was the
    odd one out and simply broke when a slug arrived.
    """
    if not ref:
        return None
    try:
        return await teams_service.get_team(session, ref)
    except TeamError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _translate(exc: PolicyError) -> HTTPException:
    if isinstance(exc, PolicyNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    # A refused edit is a permission problem, not a malformed request.
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))


def _ratio(capacity: Decimal) -> str:
    """A multiplier said the way people actually discuss it.

    "0.5" is precise and means nothing at a glance; "1 task for every 2" is what
    somebody signs off on.
    """
    if capacity <= 0:
        return "no work"
    if capacity == 1:
        return "a full share"
    if capacity < 1:
        # Rounded: 1/0.7 is 1.4285714... exactly, and a ratio quoted to
        # twenty-five decimal places reads as a bug rather than a policy.
        every = (Decimal(1) / capacity).quantize(Decimal("0.1"))
        return f"1 task for every {every.normalize():g}"
    return f"{capacity.normalize():g} tasks for every 1"


async def _out(
    session: AsyncSession, policy: AssignmentPolicy, *, user: User, roles: set[str]
) -> PolicyOut:
    body = PolicyOut.model_validate(policy)
    body.team_name = policy.team.name if policy.team else None
    body.updated_by_name = policy.updated_by.display_name if policy.updated_by else None
    allowed = await service.reach(session, user=user, roles=roles, team_id=policy.team_id)
    body.may_edit = allowed.may_edit
    body.edit_reason = None if allowed.may_edit else allowed.reason
    return body


# ── reading ────────────────────────────────────────────────────────────


@router.get("/policies", response_model=list[PolicyOut], summary="Every policy")
async def index(user: CurrentUser, roles: CurrentRoles, session: Session) -> list[PolicyOut]:
    await service.default_policy(session)  # created on first read, not by a migration
    return [
        await _out(session, policy, user=user, roles=roles)
        for policy in await service.all_policies(session)
    ]


@router.get(
    "/policies/default",
    response_model=PolicyOut,
    summary="The organisation-wide default",
)
async def default(user: CurrentUser, roles: CurrentRoles, session: Session) -> PolicyOut:
    return await _out(session, await service.default_policy(session), user=user, roles=roles)


@router.get(
    "/policies/team/{team_id}",
    response_model=PolicyOut,
    summary="The policy governing one team",
)
async def for_team(
    team_id: str, user: CurrentUser, roles: CurrentRoles, session: Session
) -> PolicyOut:
    """Their own if they have one, otherwise the organisation default.

    Check ``team_id`` on the response to tell which: null means they are running
    on the default.
    """
    team = await _team(session, team_id)
    return await _out(
        session, await service.for_team(session, team.id), user=user, roles=roles
    )


# ── editing ────────────────────────────────────────────────────────────


@router.patch(
    "/policies/default", response_model=PolicyOut, summary="Change the default"
)
async def update_default(
    payload: PolicyIn, user: CurrentUser, roles: CurrentRoles, session: Session
) -> PolicyOut:
    policy = await service.default_policy(session)
    return await _apply(session, policy, payload, user=user, roles=roles)


@router.patch(
    "/policies/team/{team_id}",
    response_model=PolicyOut,
    summary="Change one team's policy",
)
async def update_team(
    team_id: str,
    payload: PolicyIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
) -> PolicyOut:
    team = await _team(session, team_id)
    policy = await service.own_policy(session, team.id)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{team.name} has no policy of its own — it uses the organisation "
                f"default. Create one first."
            ),
        )
    return await _apply(session, policy, payload, user=user, roles=roles)


@router.post(
    "/policies/team/{team_id}",
    response_model=PolicyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Give a team its own policy",
)
async def create_team_policy(
    team_id: str, user: CurrentUser, roles: CurrentRoles, session: Session
) -> PolicyOut:
    team = await _team(session, team_id)
    try:
        await service.require_edit(session, user=user, roles=roles, team_id=team.id)
        policy = await service.create_for_team(session, team=team, actor=user)
    except PolicyError as exc:
        raise _translate(exc) from exc
    return await _out(session, policy, user=user, roles=roles)


@router.delete(
    "/policies/team/{team_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Drop a team's policy and fall back to the default",
)
async def delete_team_policy(
    team_id: str, user: CurrentUser, roles: CurrentRoles, session: Session
) -> None:
    team = await _team(session, team_id)
    policy = await service.own_policy(session, team.id)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That team has no policy of its own"
        )
    try:
        await service.require_edit(session, user=user, roles=roles, team_id=team.id)
        await service.delete_for_team(session, policy)
    except PolicyError as exc:
        raise _translate(exc) from exc


async def _apply(
    session: AsyncSession,
    policy: AssignmentPolicy,
    payload: PolicyIn,
    *,
    user: User,
    roles: set[str],
) -> PolicyOut:
    try:
        await service.require_edit(session, user=user, roles=roles, team_id=policy.team_id)
        known = {
            label.key
            for label in await labels_service.all_labels(session, team_id=policy.team_id)
        }
        await service.update(
            session,
            policy,
            actor=user,
            known_labels=known,
            **payload.model_dump(exclude_unset=True),
        )
    except PolicyError as exc:
        raise _translate(exc) from exc
    return await _out(session, policy, user=user, roles=roles)


# ── what it would mean ─────────────────────────────────────────────────


@router.get(
    "/preview",
    response_model=PolicyPreviewOut,
    summary="What the policy means for real people",
)
async def preview(
    _: CurrentUser,
    session: Session,
    team: Annotated[
        str | None,
        Query(description="Team handle or id; omit for the organisation default"),
    ] = None,
) -> PolicyPreviewOut:
    """Capacity and eligibility, person by person, as of today.

    This is where a ratio stops being an abstract number: it shows who is
    actually in the pool, at what share, and who is out and why. Nothing is
    assigned and nothing is written.
    """
    resolved = await _team(session, team)
    team_id = resolved.id if resolved else None
    policy = await service.for_team(session, team_id)

    query = select(User).where(User.is_active.is_(True)).order_by(User.display_name)
    if team_id is not None:
        query = query.join(TeamMembership, TeamMembership.user_id == User.id).where(
            TeamMembership.team_id == team_id
        )
    people = list((await session.scalars(query)).all())

    held = await labels_service.effective_labels(
        session,
        people,
        team_id=team_id,
        new_joiner_days=policy.new_joiner_days,
        new_joiner_from_first_seen=policy.new_joiner_from_first_seen,
    )

    effects: list[EffectOut] = []
    for person in people:
        keys = {label.key for label in held.get(person.id, [])}
        capacity = service.capacity_for(policy, keys)
        reason = service.is_excluded(policy, keys)
        # The leave exclusion is a policy switch, not a label lookup, so it is
        # applied here rather than baked into is_excluded.
        if reason is None and policy.exclude_on_leave and "on-leave" in keys:
            reason = "On approved leave today"

        effects.append(
            EffectOut(
                user_id=person.id,
                display_name=person.display_name,
                labels=sorted(keys),
                capacity=capacity,
                ratio=_ratio(capacity),
                max_open=service.max_open_for(policy, keys),
                excluded=reason is not None,
                excluded_reason=reason,
            )
        )

    return PolicyPreviewOut(
        policy_id=policy.id,
        inherited=policy.team_id is None and team_id is not None,
        team_id=team_id,
        people=effects,
        assignable=sum(1 for e in effects if not e.excluded),
    )
