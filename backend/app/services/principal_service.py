"""Load a :class:`Principal` from the database.

Three queries per request build the caller's full authorization picture. The legacy system
read ``SUPERUSERS`` from a SharePoint list at import time and refreshed it from a background
thread; this reads the current row, every time, inside the request's transaction.

**Why every query here pins its loader strategy.** ``User.memberships``,
``User.label_assignments``, ``Role.permissions`` and ``Membership.role`` are all declared
``lazy="selectin"`` on the models, so a bare ``select(User)`` quietly fans out into a
cascade of follow-up SELECTs — user, memberships, roles, role permissions, label
assignments, labels. Seven round trips to answer "who is this".

That is invisible against a local Postgres and brutal against a remote one: this endpoint
runs on *every* page navigation, so at 300 ms of network latency it alone cost about four
seconds before the ``lazyload("*")`` calls below shut the cascade off. Each query now
declares exactly what it needs and joins it in one trip.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, lazyload

from app.core.principal import Principal, TeamGrant
from app.core.rbac import Scope
from app.models.identity import Membership, Role, Team, User, UserStatus
from app.models.labels import LabelAssignment

SUPER_ADMIN_ROLE_KEY = "super_admin"


async def load_principal(session: AsyncSession, user_id: uuid.UUID) -> Principal | None:
    """Build the principal for ``user_id``, or None if absent or deactivated."""
    # lazyload("*") suppresses the model-level selectin cascade: nothing here reads
    # user.memberships or user.label_assignments, and both are fetched properly below.
    user = await session.scalar(
        select(User).where(User.id == user_id).options(lazyload("*"))
    )
    if user is None or user.status is not UserStatus.ACTIVE:
        return None

    memberships = (
        (
            await session.scalars(
                select(Membership)
                .where(Membership.user_id == user_id)
                .options(
                    lazyload("*"),
                    joinedload(Membership.role).joinedload(Role.permissions),
                    joinedload(Membership.team),
                )
            )
        )
        # joinedload against a collection multiplies rows; unique() collapses the identities.
        .unique()
        .all()
    )

    is_super_admin = False
    org_pairs: list[tuple[str, Scope]] = []
    teams: dict[uuid.UUID, TeamGrant] = {}

    for membership in memberships:
        role = membership.role
        team = membership.team
        if team is None or team.archived_at is not None:
            continue

        if role.key == SUPER_ADMIN_ROLE_KEY:
            is_super_admin = True

        pairs = [(rp.permission_key, Scope(rp.scope)) for rp in role.permissions]

        if role.is_team_scoped:
            teams[team.id] = TeamGrant(
                team_id=team.id,
                team_slug=team.slug,
                role_key=role.key,
                permissions=Principal.merge_scopes(pairs),
            )
        else:
            # An org-scoped role still implies membership of the team it was granted through,
            # so the user shows up in that team's UI — but its permissions apply everywhere.
            org_pairs.extend(pairs)
            teams.setdefault(
                team.id,
                TeamGrant(
                    team_id=team.id,
                    team_slug=team.slug,
                    role_key=role.key,
                    permissions={},
                ),
            )

    if is_super_admin:
        # A super admin is implicitly in every team. Their permissions already apply
        # everywhere (Principal.has short-circuits), but Principal.teams is what the UI
        # enumerates for team pickers and what teams_with() filters on — so without this a
        # super admin would be unable to reach a department they hold no membership row in.
        teams = await _with_every_team(session, teams)

    return Principal(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_super_admin=is_super_admin,
        org_permissions=Principal.merge_scopes(org_pairs),
        teams=teams,
        labels=await _load_labels(session, user_id),
    )


async def _with_every_team(
    session: AsyncSession, held: dict[uuid.UUID, TeamGrant]
) -> dict[uuid.UUID, TeamGrant]:
    """Add every active team the user has no membership row for.

    Permissions stay empty: they are never consulted, because ``is_super_admin`` answers
    first. This exists so the team *appears*, not to grant anything.
    """
    complete = dict(held)
    for team in (
        await session.scalars(
            select(Team).where(Team.archived_at.is_(None)).options(lazyload("*"))
        )
    ).all():
        complete.setdefault(
            team.id,
            TeamGrant(
                team_id=team.id,
                team_slug=team.slug,
                role_key=SUPER_ADMIN_ROLE_KEY,
                permissions={},
            ),
        )
    return complete


async def _load_labels(
    session: AsyncSession, user_id: uuid.UUID
) -> dict[uuid.UUID | None, frozenset[str]]:
    """Active label keys, grouped by team (org-wide labels under ``None``).

    Expiry is filtered here rather than by a reaper job, so there is no window in which a
    stale ``new-joiner`` label is still shrinking someone's workload.
    """
    now = datetime.now(UTC)
    assignments = (
        (
            await session.scalars(
                select(LabelAssignment)
                .where(LabelAssignment.user_id == user_id)
                .options(lazyload("*"), joinedload(LabelAssignment.label))
            )
        )
        .unique()
        .all()
    )

    grouped: dict[uuid.UUID | None, set[str]] = {}
    for assignment in assignments:
        if not assignment.is_active(now=now):
            continue
        if assignment.label is None:
            continue
        grouped.setdefault(assignment.team_id, set()).add(assignment.label.key)

    return {team_id: frozenset(keys) for team_id, keys in grouped.items()}
