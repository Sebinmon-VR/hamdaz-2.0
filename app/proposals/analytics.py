"""Per-person proposal workload, for admins.

Why this is one computation rather than several endpoints:

SharePoint cannot aggregate. ``$apply=groupby(...)`` is accepted and *silently
ignored* — it returns ordinary rows with a 200 — and ``$count`` is unsupported.
So every one of these numbers requires pulling the whole list and counting it
here. Five separate metric endpoints would mean five identical sweeps; one sweep
produces all of them at once.

Nor is it computed in the browser. The aggregate is ~2 KB; the rows behind it are
~400 KB, and shipping them would put every proposal in the company into every
admin's browser to work out an average.

The result is identical for every admin, so it is cached process-wide for a short
while. Cold it costs about a second and a half; warm, nothing.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.proposals.sharepoint import ProposalTask, SharePointProposals

#: The aggregate is the same for everyone who may see it, so one cache serves
#: all admins. Short enough that a change in SharePoint shows up promptly.
CACHE_TTL_SECONDS: Final = 60

#: "Due soon" horizon. Chosen because the data is bimodal: open tasks are either
#: already past their bid closing date or within days of it.
SOON_DAYS: Final = 7


def parse_when(value: str | None) -> datetime | None:
    """A SharePoint timestamp as a datetime, or None if it is unusable.

    Public because the team-tasks view next door needs the identical reading of
    a deadline; two modules parsing these strings slightly differently is how a
    row ends up counted as due on one screen and not on the other.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # SharePoint's are always aware. One built elsewhere without a zone is
    # taken as UTC rather than left to blow up the first comparison.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(slots=True)
class PersonWorkload:
    lookup_id: str | None
    name: str
    email: str | None
    total: int = 0
    completed: int = 0
    #: Never given a status, and the bid closed. Counted as finished rather than
    #: as somebody's live workload — see ProposalTask.effective_status. Reported
    #: on its own so the derivation can be checked rather than trusted.
    expired: int = 0
    #: Not finished, but the bid closed. Not current workload, and reported on
    #: its own because it is where nearly all of this list actually sits.
    bid_closed: int = 0
    #: Not finished and the bid has not closed. **The number the scoring uses.**
    active: int = 0
    open: int = 0
    overdue: int = 0
    due_soon: int = 0
    later: int = 0
    no_deadline: int = 0
    #: A fifth of the list has no Status at all. Counted in `open`, but also
    #: reported on its own so the backlog is not silently overstated.
    no_status: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    #: The nearest bid closing date still ahead of them.
    next_deadline: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "lookup_id": self.lookup_id,
            "name": self.name,
            "email": self.email,
            "total": self.total,
            "completed": self.completed,
            "expired": self.expired,
            "bid_closed": self.bid_closed,
            "active": self.active,
            "open": self.open,
            "overdue": self.overdue,
            "due_soon": self.due_soon,
            "later": self.later,
            "no_deadline": self.no_deadline,
            "no_status": self.no_status,
            "by_status": dict(sorted(self.by_status.items())),
            "next_deadline": self.next_deadline,
        }


def _bucket(task: ProposalTask, now: datetime, soon: datetime) -> str:
    when = parse_when(task.deadline)
    if when is None:
        return "no_deadline"
    if when < now:
        return "overdue"
    if when <= soon:
        return "due_soon"
    return "later"


def summarise(
    tasks: list[ProposalTask],
    people: dict[str, dict[str, str]],
    *,
    now: datetime | None = None,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """Turn the list into per-person counts and a total.

    ``only`` restricts the result to those SharePoint lookup ids — used to scope
    the numbers to one team's members. Everything left out is *counted and
    reported* rather than silently dropped: a workload view that quietly hides
    four hundred overdue tasks is worse than one that shows none.

    Pure and synchronous, so it is testable without touching SharePoint.
    """
    now = now or datetime.now(UTC)
    soon = now + timedelta(days=SOON_DAYS)

    by_person: dict[str | None, PersonWorkload] = {}
    org = PersonWorkload(lookup_id=None, name="Organisation", email=None)
    excluded_rows = 0
    excluded_people: dict[str | None, str] = {}

    for task in tasks:
        key = task.assigned_to_lookup_id
        if only is not None and (key is None or key not in only):
            excluded_rows += 1
            # Same name resolution as the included path: user list first, then
            # the display name on the row.
            excluded_people.setdefault(
                key,
                people.get(key or "", {}).get("name")
                or task.assigned_to_name
                or "Unassigned",
            )
            continue
        person = by_person.get(key)
        if person is None:
            who = people.get(key or "", {})
            person = PersonWorkload(
                lookup_id=key,
                # A row can be assigned to nobody; that is worth seeing, not hiding.
                name=who.get("name") or (task.assigned_to_name or "Unassigned"),
                email=who.get("email") or None,
            )
            by_person[key] = person

        for target in (person, org):
            target.total += 1
            if task.has_no_status:
                target.no_status += 1
            status = task.status or "(no status)"
            target.by_status[status] = target.by_status.get(status, 0) + 1

            if task.is_expired:
                target.expired += 1
                continue
            if not task.is_open:
                target.completed += 1
                continue

            target.open += 1
            if task.is_active:
                target.active += 1
            else:
                target.bid_closed += 1
            bucket = _bucket(task, now, soon)
            setattr(target, bucket, getattr(target, bucket) + 1)

            if bucket in ("due_soon", "later"):
                deadline = task.deadline
                if deadline and (target.next_deadline is None or deadline < target.next_deadline):
                    target.next_deadline = deadline

    # Busiest first: an admin opening this wants the overloaded people at the top.
    people_out = sorted(
        (p.as_dict() for p in by_person.values()),
        key=lambda p: (-p["overdue"], -p["open"], p["name"].casefold()),
    )
    return {
        "organisation": org.as_dict(),
        "people": people_out,
        "person_count": len(people_out),
        "soon_days": SOON_DAYS,
        "generated_at": now.isoformat(),
        "excluded": {
            "rows": excluded_rows,
            "people": len(excluded_people),
            # Named so it is obvious who is missing and why.
            "names": sorted(set(excluded_people.values()))[:25],
        },
    }


async def scope_for_team(
    session, team, sharepoint: SharePointProposals
) -> dict[str, Any]:
    """Map an ERP team's members onto SharePoint lookup ids.

    The join is by email, the only identifier the two systems share. A member
    with no SharePoint presence is named rather than dropped — that is usually
    the explanation for a number looking too low.
    """
    from app.teams import service as teams_service

    members = await teams_service.list_members(session, team.id)
    site_users = await sharepoint.site_users()

    def resolve(users: dict[str, str]) -> tuple[set[str], list[str]]:
        found_ids: set[str] = set()
        missing: list[str] = []
        for user, _rows in members:
            hit = users.get(user.email.casefold())
            if hit:
                found_ids.add(hit)
            else:
                missing.append(user.email)
        return found_ids, missing

    lookup_ids, unmatched = resolve(site_users)

    if unmatched:
        # Somebody added to the SharePoint site since the user cache was filled
        # would otherwise stay invisible for up to fifteen minutes. Membership
        # changes should show up on the next call, not eventually.
        lookup_ids, unmatched = resolve(await sharepoint.site_users(force=True))

    matched = len(members) - len(unmatched)

    return {
        "team_slug": team.slug,
        "team_name": team.name,
        "member_count": len(members),
        "matched_in_sharepoint": matched,
        # Named so "why is this empty" has an answer on the screen.
        "members_without_sharepoint": sorted(unmatched),
        "lookup_ids": lookup_ids,
    }


class WorkloadCache:
    """One cached sweep of the list, summarised per request.

    The *raw rows* are cached rather than the finished summary, because the
    summary now depends on which team is asking. Counting 1,291 in-memory rows
    costs microseconds; fetching them costs two seconds, so the expensive half is
    what gets shared.
    """

    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._raw: tuple[list[ProposalTask], dict[str, dict[str, str]]] | None = None
        self._at = 0.0
        self._fetch_ms = 0

    def _fresh(self) -> bool:
        return self._raw is not None and (time.monotonic() - self._at) < self._ttl

    async def _rows(
        self, sharepoint: SharePointProposals, *, refresh: bool
    ) -> tuple[list[ProposalTask], dict[str, dict[str, str]], bool]:
        if self._fresh() and not refresh:
            return (*self._raw, True)  # type: ignore[misc]

        started = time.monotonic()
        # Independent calls, so run them together rather than back to back.
        tasks, people = await asyncio.gather(
            sharepoint.all_tasks(), sharepoint.site_people()
        )
        self._raw = (tasks, people)
        self._at = time.monotonic()
        self._fetch_ms = int((time.monotonic() - started) * 1000)
        return tasks, people, False

    async def get(
        self,
        sharepoint: SharePointProposals,
        *,
        refresh: bool = False,
        only: set[str] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        tasks, people, cached = await self._rows(sharepoint, refresh=refresh)

        summary = summarise(tasks, people, only=only)
        summary["row_count"] = len(tasks)
        summary["cached"] = cached
        summary["age_seconds"] = int(time.monotonic() - self._at)
        summary["fetch_ms"] = 0 if cached else self._fetch_ms
        summary["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return summary

    def invalidate(self) -> None:
        self._raw, self._at = None, 0.0
