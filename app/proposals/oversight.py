"""A team's proposal tasks, row by row, for the people who run that team.

``/my-tasks`` answers "what am I carrying" and refuses to answer it about
anybody else — deliberately, and the router says so in as many words. This
module is the "separate, explicitly authorised addition" that docstring
anticipated: the same rows, for a whole team, shown to the people accountable
for that team's work.

Three things keep it honest:

**Whose rows.** The set of people is derived from the team's membership in this
database, joined to SharePoint by email. No request parameter names a person,
so a caller cannot widen the answer to somebody they have no authority over —
the property that makes ``/my-tasks`` safe, applied one level up.

**Who may ask.** :func:`oversight` decides, following the shape of
``app.assignment.service.reach``: authority is either organisation-wide or held
inside the one team. A team lead sees their own team and is refused on every
other, which is what makes the team-scoped role worth holding rather than a
quiet synonym for admin.

**What it costs.** One filtered Graph query per member, run together rather
than in sequence, cached per team for a minute. The workload cache next door
cannot be reused for this: it sweeps the list with ``_AGGREGATE_FIELDS`` — four
columns — because counting needs nothing more. A task list needs every column,
so this fetches its own rows.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.models.user import User
from app.proposals.analytics import SOON_DAYS, parse_when
from app.proposals.sharepoint import ProposalTask, SharePointProposals
from app.roles.catalogue import ADMIN_ROLES

#: Organisation-wide roles that reach every team. The same set that guards
#: ``/workload``, so the aggregate and the rows behind it agree on who counts
#: as an administrator.
GLOBAL_OVERSIGHT: Final[frozenset[str]] = ADMIN_ROLES

#: Roles held *inside* a team that carry the same right for that team alone.
#: This is how a lead sees their own people without any reach over anyone
#: else's — the distinction the team-scoped roles exist to make.
TEAM_OVERSIGHT: Final[frozenset[str]] = frozenset({"team_manager", "team_lead"})

#: Same TTL as the workload aggregate. Long enough that a lead refreshing the
#: page does not re-query Graph once per member each time; short enough that a
#: row edited in SharePoint appears while they are still looking at it.
CACHE_TTL_SECONDS: Final = 60

#: Graph is asked for one member's rows at a time. Running the team together is
#: what makes this a second rather than a minute, but an unbounded fan-out on a
#: forty-person team would be rude to a tenant shared with everything else.
MAX_CONCURRENT_FETCHES: Final = 8


class OversightError(Exception):
    """The caller may not see this team's tasks. Safe to show a user."""


@dataclass(frozen=True, slots=True)
class Oversight:
    """Whether one person may see one team's rows, and why."""

    may_see: bool
    reason: str


async def oversight(session, *, user: User, roles: set[str], team_id: uuid.UUID) -> Oversight:
    """Decide whether ``user`` may see the individual tasks of ``team_id``.

    Deliberately *not* gated on the proposals module as well. ``require_module``
    on ``/my-tasks`` asks "has your team been granted this feature", which is a
    question about the viewer's own workspace. This is a management view of
    somebody else's, so the gate that matters is authority over the people whose
    rows are being shown — the same reasoning that leaves ``/workload`` resting
    on ``AdminUser`` alone.
    """
    if roles & GLOBAL_OVERSIGHT:
        return Oversight(True, "Super admins, the CEO and managers see every team.")

    from app.teams.service import team_role_keys

    held = await team_role_keys(session, team_id=team_id, user_id=user.id)
    if held & TEAM_OVERSIGHT:
        return Oversight(True, "Team lead or team manager of this team.")

    return Oversight(
        False,
        "Only this team's lead or manager, or an administrator, may see its "
        "members' individual proposal tasks.",
    )


@dataclass(frozen=True, slots=True)
class MemberRef:
    """One member of the team, as SharePoint can be asked about them."""

    user_id: uuid.UUID
    name: str
    email: str
    role_keys: list[str]
    #: None when they have no presence on the SharePoint site at all — a
    #: different thing from having nothing assigned, and reported as such.
    lookup_id: str | None


async def resolve_members(session, team, sharepoint: SharePointProposals) -> list[MemberRef]:
    """The team's members, each joined to their SharePoint lookup id.

    Email is the only identifier the two systems share, so it is the join.
    Somebody added to the SharePoint site since its user cache was filled would
    otherwise stay invisible for up to fifteen minutes, so an unmatched member
    forces one re-read — the same correction ``scope_for_team`` makes.
    """
    from app.teams import service as teams_service

    members = await teams_service.list_members(session, team.id)
    site_users = await sharepoint.site_users()

    def build(users: dict[str, str]) -> list[MemberRef]:
        return [
            MemberRef(
                user_id=user.id,
                name=user.display_name,
                email=user.email,
                role_keys=sorted(row.role.key for row in rows),
                lookup_id=users.get(user.email.casefold()),
            )
            for user, rows in members
        ]

    resolved = build(site_users)
    if any(m.lookup_id is None for m in resolved):
        resolved = build(await sharepoint.site_users(force=True))
    return resolved


def _summarise_member(
    member: MemberRef,
    tasks: list[ProposalTask],
    *,
    open_only: bool,
    now: datetime,
    soon: datetime,
) -> dict[str, Any]:
    """One member's rows, counted and ordered the way ``/my-tasks`` does it.

    ``now`` governs the deadline arithmetic here, but **not** ``open_count`` and
    ``active_count``: those come from ``ProposalTask.is_open`` and ``.is_active``,
    which read the wall clock themselves and cannot be told otherwise.
    ``analytics.summarise`` has the same split. In production the two are the
    same instant so nothing diverges, but a caller passing a synthetic ``now``
    gets it honoured by half — which is worth knowing before writing a test
    against it.
    """
    open_tasks = [t for t in tasks if t.is_open]
    active = [t for t in tasks if t.is_active]
    shown = open_tasks if open_only else tasks

    # Soonest deadline first, by BCD rather than DueDate — see ProposalTask.deadline.
    shown = sorted(shown, key=lambda t: (t.deadline is None, t.deadline or ""))

    # Only deadlines still ahead of them. A bid that closed last March is not
    # "next", however near the top of the sort it lands.
    ahead: list[str] = []
    due_soon = 0
    for task in active:
        when = parse_when(task.deadline)
        if when is None or task.deadline is None:
            continue
        if when >= now:
            ahead.append(task.deadline)
            if when <= soon:
                due_soon += 1

    return {
        "user_id": member.user_id,
        "name": member.name,
        "email": member.email,
        "role_keys": member.role_keys,
        "sharepoint_user_id": member.lookup_id,
        "in_sharepoint": member.lookup_id is not None,
        "total": len(tasks),
        "open_count": len(open_tasks),
        # Not finished *and* the bid is still open. The number a lead acts on:
        # `open` on this list is mostly an archive of bids that closed months
        # ago, so leading with it describes a crisis that is not happening.
        # See ProposalTask.is_active.
        "active_count": len(active),
        "due_soon_count": due_soon,
        "next_deadline": min(ahead) if ahead else None,
        "tasks": shown,
    }


class TeamTasksCache:
    """One cached fan-out per team.

    The *rows* are cached rather than the finished response, because
    ``open_only`` changes the response and nothing else. Filtering an in-memory
    list costs microseconds; asking Graph once per member costs the better part
    of a second, so the expensive half is what gets shared.
    """

    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._by_team: dict[uuid.UUID, tuple[dict[str, list[ProposalTask]], float]] = {}

    async def rows(
        self,
        sharepoint: SharePointProposals,
        *,
        team_id: uuid.UUID,
        members: list[MemberRef],
        limit: int,
        refresh: bool,
    ) -> tuple[dict[str, list[ProposalTask]], bool, int]:
        """``lookup_id -> rows``, plus whether it was cached and how old."""
        hit = self._by_team.get(team_id)
        if hit is not None and not refresh:
            cached_rows, at = hit
            age = int(time.monotonic() - at)
            # Somebody added to the team since the sweep would be absent from
            # it, which reads on screen as "they have nothing" rather than as
            # "this is stale". Re-fetch rather than answer wrongly.
            complete = all(m.lookup_id in cached_rows for m in members if m.lookup_id)
            if age < self._ttl and complete:
                return cached_rows, True, age

        gate = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def fetch(lookup_id: str) -> tuple[str, list[ProposalTask]]:
            async with gate:
                return lookup_id, await sharepoint.tasks_assigned_to(lookup_id, limit=limit)

        wanted = sorted({m.lookup_id for m in members if m.lookup_id})
        fetched = await asyncio.gather(*(fetch(lid) for lid in wanted))

        rows = dict(fetched)
        self._by_team[team_id] = (rows, time.monotonic())
        return rows, False, 0

    def invalidate(self, team_id: uuid.UUID | None = None) -> None:
        if team_id is None:
            self._by_team.clear()
        else:
            self._by_team.pop(team_id, None)


async def team_tasks(
    session,
    *,
    team,
    sharepoint: SharePointProposals,
    cache: TeamTasksCache,
    open_only: bool = False,
    limit: int = 200,
    refresh: bool = False,
) -> dict[str, Any]:
    """Every member's rows, ordered so the person under most pressure leads."""
    now = datetime.now(UTC)
    soon = now + timedelta(days=SOON_DAYS)

    members = await resolve_members(session, team, sharepoint)
    rows, cached, age = await cache.rows(
        sharepoint, team_id=team.id, members=members, limit=limit, refresh=refresh
    )

    summarised = [
        _summarise_member(
            member,
            rows.get(member.lookup_id or "", []),
            open_only=open_only,
            now=now,
            soon=soon,
        )
        for member in members
    ]

    # Busiest first, on live work rather than on the archive — the reason to
    # open this screen is finding who needs help this week. Anyone carrying
    # nothing live sorts by name among the rest rather than vanishing.
    summarised.sort(
        key=lambda m: (-m["active_count"], -m["due_soon_count"], m["name"].casefold())
    )

    unmatched = sorted(m.email for m in members if m.lookup_id is None)

    return {
        "scope": {
            "team_slug": team.slug,
            "team_name": team.name,
            "member_count": len(members),
            "matched_in_sharepoint": len(members) - len(unmatched),
            # Named so "why is this empty" has an answer on the screen.
            "members_without_sharepoint": unmatched,
        },
        "soon_days": SOON_DAYS,
        "generated_at": now.isoformat(),
        "member_count": len(members),
        "total": sum(m["total"] for m in summarised),
        "open_count": sum(m["open_count"] for m in summarised),
        "active_count": sum(m["active_count"] for m in summarised),
        "members": summarised,
        "cached": cached,
        "age_seconds": age,
    }
