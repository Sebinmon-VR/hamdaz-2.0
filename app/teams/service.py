"""Teams and their membership.

The invariants enforced here, rather than in the router, so they hold however
the call arrives:

* a slug is unique and generated from the name when not supplied
* only ``scope="team"`` roles can be held inside a team
* every member has at least one role — a membership with none is not a member
* archiving is reversible; deleting is not, and refuses to run on a live team
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.role import Role, RoleScope
from app.models.team import Team, TeamMembership, slugify
from app.models.user import User
from app.roles.catalogue import DEFAULT_TEAM_ROLE, TEAM_LEAD


class TeamError(Exception):
    """A team operation was refused. The message is safe to show a user."""


class TeamNotFoundError(TeamError):
    pass


class TeamConflictError(TeamError):
    """The request contradicts an invariant — duplicate slug, unknown role, …"""


# ── teams ──────────────────────────────────────────────────────────────


async def _unique_slug(session: AsyncSession, desired: str) -> str:
    """``desired``, or ``desired-2``, ``desired-3`` … until it is free."""
    base = desired or "team"
    slug, n = base, 1
    while await session.scalar(select(Team.id).where(Team.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


async def create_team(
    session: AsyncSession,
    *,
    name: str,
    description: str | None = None,
    slug: str | None = None,
    created_by_id: uuid.UUID | None = None,
) -> Team:
    name = name.strip()
    if not name:
        raise TeamError("A team needs a name")

    if slug:
        wanted = slugify(slug)
        if await session.scalar(select(Team.id).where(Team.slug == wanted)):
            # An explicit slug is a deliberate choice, so clashing is an error
            # rather than something to silently rename.
            raise TeamConflictError(f"A team with the handle {wanted!r} already exists")
        final = wanted
    else:
        final = await _unique_slug(session, slugify(name))

    team = Team(
        name=name, slug=final, description=description, created_by_id=created_by_id
    )
    session.add(team)
    await session.flush()
    return team


async def get_team(session: AsyncSession, ref: str | uuid.UUID) -> Team:
    """Fetch by uuid or by slug — both appear in URLs."""
    try:
        as_uuid: uuid.UUID | None = uuid.UUID(str(ref))
    except (ValueError, AttributeError):
        as_uuid = None

    team = None
    if as_uuid is not None:
        team = await session.get(Team, as_uuid)
    if team is None:
        team = await session.scalar(select(Team).where(Team.slug == str(ref)))
    if team is None:
        raise TeamNotFoundError(f"No team {str(ref)!r}")
    return team


async def list_teams(
    session: AsyncSession, *, search: str | None = None, include_archived: bool = False
) -> list[Team]:
    query = select(Team).order_by(Team.name)
    if not include_archived:
        query = query.where(Team.archived_at.is_(None))
    if search:
        needle = f"%{search.strip().casefold()}%"
        query = query.where(
            func.lower(Team.name).like(needle) | func.lower(Team.slug).like(needle)
        )
    return list((await session.scalars(query)).all())


async def update_team(
    session: AsyncSession,
    ref: str | uuid.UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    slug: str | None = None,
) -> Team:
    team = await get_team(session, ref)
    if name is not None:
        if not name.strip():
            raise TeamError("A team needs a name")
        team.name = name.strip()
    if description is not None:
        team.description = description
    if slug is not None:
        wanted = slugify(slug)
        clash = await session.scalar(
            select(Team.id).where(Team.slug == wanted, Team.id != team.id)
        )
        if clash:
            raise TeamConflictError(f"A team with the handle {wanted!r} already exists")
        team.slug = wanted
    await session.flush()
    return team


async def archive_team(session: AsyncSession, ref: str | uuid.UUID) -> Team:
    team = await get_team(session, ref)
    if team.archived_at is None:
        team.archived_at = datetime.now(UTC)
        await session.flush()
    return team


async def restore_team(session: AsyncSession, ref: str | uuid.UUID) -> Team:
    team = await get_team(session, ref)
    team.archived_at = None
    await session.flush()
    return team


async def delete_team(session: AsyncSession, ref: str | uuid.UUID) -> None:
    """Permanently remove a team and its memberships.

    Only an archived team can be deleted. Requiring the archive step first means
    a live team cannot be destroyed by one mistaken call, and gives anyone who
    noticed a window to restore it.
    """
    team = await get_team(session, ref)
    if team.archived_at is None:
        raise TeamConflictError(
            f"{team.slug!r} is still active — archive it first, then delete"
        )
    await session.delete(team)
    await session.flush()


# ── membership ─────────────────────────────────────────────────────────


async def _team_roles(session: AsyncSession, keys: Iterable[str]) -> list[Role]:
    wanted = [k.strip() for k in keys if k and k.strip()]
    if not wanted:
        wanted = [DEFAULT_TEAM_ROLE]

    found = {
        r.key: r for r in (await session.scalars(select(Role).where(Role.key.in_(wanted)))).all()
    }
    missing = [k for k in wanted if k not in found]
    if missing:
        raise TeamNotFoundError(f"No such role: {', '.join(sorted(missing))}")

    global_ones = [k for k in wanted if found[k].scope != RoleScope.TEAM]
    if global_ones:
        raise TeamConflictError(
            f"{', '.join(sorted(global_ones))} is an organisation-wide role — "
            f"grant it under /roles, not inside a team"
        )
    # dict.fromkeys keeps the caller's order while dropping repeats.
    return [found[k] for k in dict.fromkeys(wanted)]


async def set_member_roles(
    session: AsyncSession,
    *,
    team: Team,
    user: User,
    role_keys: Iterable[str],
    added_by_id: uuid.UUID | None = None,
) -> list[TeamMembership]:
    """Make the user's roles in this team exactly ``role_keys``.

    Used for both joining and changing: "set" rather than "add" means the caller
    never has to work out which individual grants to add and which to remove,
    and repeating a call cannot drift the result.
    """
    roles = await _team_roles(session, role_keys)
    wanted = {r.id: r for r in roles}

    existing = list(
        (
            await session.scalars(
                select(TeamMembership).where(
                    TeamMembership.team_id == team.id, TeamMembership.user_id == user.id
                )
            )
        ).all()
    )

    for row in existing:
        if row.role_id not in wanted:
            await session.delete(row)

    held = {row.role_id for row in existing}
    for role_id in wanted:
        if role_id not in held:
            session.add(
                TeamMembership(
                    team_id=team.id,
                    user_id=user.id,
                    role_id=role_id,
                    added_by_id=added_by_id,
                )
            )

    await session.flush()
    return await member_rows(session, team.id, user.id)


async def remove_member(session: AsyncSession, *, team: Team, user_id: uuid.UUID) -> None:
    rows = await member_rows(session, team.id, user_id)
    if not rows:
        raise TeamNotFoundError("That user is not in this team")
    for row in rows:
        await session.delete(row)
    await session.flush()


async def member_rows(
    session: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID
) -> list[TeamMembership]:
    return list(
        (
            await session.scalars(
                select(TeamMembership)
                .where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
            )
        ).all()
    )


async def list_members(
    session: AsyncSession, team_id: uuid.UUID
) -> list[tuple[User, list[TeamMembership]]]:
    """Everyone in the team, each with the set of roles they hold in it."""
    rows = (
        await session.scalars(
            select(TeamMembership).where(TeamMembership.team_id == team_id)
        )
    ).all()

    grouped: dict[uuid.UUID, list[TeamMembership]] = {}
    for row in rows:
        grouped.setdefault(row.user_id, []).append(row)

    members = [(rows_[0].user, rows_) for rows_ in grouped.values()]
    members.sort(key=lambda pair: pair[0].display_name.casefold())
    return members


async def member_counts(
    session: AsyncSession, team_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Distinct member count per team — one query, not one per team."""
    ids = list(team_ids)
    if not ids:
        return {}
    rows = await session.execute(
        select(TeamMembership.team_id, func.count(func.distinct(TeamMembership.user_id)))
        .where(TeamMembership.team_id.in_(ids))
        .group_by(TeamMembership.team_id)
    )
    counts = dict(rows.all())
    return {tid: counts.get(tid, 0) for tid in ids}


async def teams_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, include_archived: bool = False
) -> list[tuple[Team, list[str]]]:
    query = (
        select(TeamMembership)
        .join(Team, Team.id == TeamMembership.team_id)
        .where(TeamMembership.user_id == user_id)
        # Team is joined explicitly; role and user already load eagerly.
        .options(joinedload(TeamMembership.team))
        .order_by(Team.name)
    )
    if not include_archived:
        query = query.where(Team.archived_at.is_(None))

    grouped: dict[uuid.UUID, tuple[Team, list[str]]] = {}
    for row in (await session.scalars(query)).all():
        entry = grouped.setdefault(row.team_id, (row.team, []))
        entry[1].append(row.role.key)
    return [(team, sorted(keys)) for team, keys in grouped.values()]


async def team_role_keys(
    session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID
) -> set[str]:
    keys = await session.scalars(
        select(Role.key)
        .join(TeamMembership, TeamMembership.role_id == Role.id)
        .where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
    )
    return set(keys.all())


async def leads(session: AsyncSession, team_id: uuid.UUID) -> list[User]:
    rows = await session.scalars(
        select(TeamMembership)
        .join(Role, Role.id == TeamMembership.role_id)
        .where(TeamMembership.team_id == team_id, Role.key == TEAM_LEAD)
    )
    return [r.user for r in rows.all()]
