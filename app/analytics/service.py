"""Gathering the inputs, running the score, and keeping the result.

Where each input comes from:

* **task counts** — read live from the SharePoint Proposals list on every run.
  Read-only, every call a GET. Nothing is written to SharePoint, and the
  ``testuseranalytics`` list is not touched at all; results are kept in Postgres.
* **labels** — from ``app.labels.service``, including the two that are derived
  rather than stored, so somebody on approved leave drops out of the ranking on
  the day their leave starts.
* **capacity, limits and weights** — from the assignment policy for that team.

Matching people is the awkward part and is done on email. SharePoint knows a
person by a site-local lookup id and a display name; this app knows them by
their Entra account. Display names collide and change, so email is the only
identifier both sides agree on — and anybody who cannot be matched is still
counted, because leaving them out would understate the team's real load.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.scoring import Candidate, Scored, score
from app.assignment import service as policy_service
from app.labels import service as labels_service
from app.models.analytics import AnalyticsRun, UserAnalytics
from app.models.assignment import AssignmentPolicy
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.proposals.analytics import WorkloadCache
from app.proposals.sharepoint import SharePointError, SharePointProposals

logger = logging.getLogger("hamdaz.analytics")


class AnalyticsError(Exception):
    """A run could not be produced. Safe to show a user."""


class AnalyticsNotFoundError(AnalyticsError):
    pass


class NotInScopeError(AnalyticsError):
    """This team does not distribute work through the assignment scoring."""


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def _last_assigned(
    sharepoint: SharePointProposals,
) -> dict[str, datetime]:
    """When each person was last given work, by SharePoint lookup id.

    The list has no "assigned on" column, so the newest row assigned to somebody
    is the closest honest proxy — for this list a row appearing *is* the work
    being handed out. Named plainly rather than hidden, because a proxy that
    nobody knows is a proxy is the sort of thing that later surprises people.
    """
    newest: dict[str, datetime] = {}
    for task in await sharepoint.all_tasks():
        key = task.assigned_to_lookup_id
        created = _parse(task.created_at)
        if key is None or created is None:
            continue
        if key not in newest or created > newest[key]:
            newest[key] = created
    return newest


async def gather(
    session: AsyncSession,
    sharepoint: SharePointProposals,
    cache: WorkloadCache,
    *,
    team: Team | None = None,
    refresh: bool = False,
    workload: dict[str, Any] | None = None,
    last_seen: dict[str, datetime] | None = None,
) -> tuple[list[Candidate], AssignmentPolicy, dict[str, Any]]:
    """Everything the score needs, for one team or for everybody.

    ``workload`` and ``last_seen`` may be supplied by a caller that already has
    them, in which case SharePoint is not touched at all. That is how the live
    scoring in ``app.analytics.live`` reuses every rule below — capacity by
    label, on-leave, the manager exclusion, the open-work ceiling — while
    sourcing its counts from the local mirror. Two rankings that disagreed
    would be worse than one that is occasionally a minute out of date, so
    there is deliberately only one copy of this.
    """
    if workload is None:
        try:
            workload = await cache.get(sharepoint, refresh=refresh)
            last_seen = await _last_assigned(sharepoint)
        except SharePointError as exc:
            raise AnalyticsError(f"Could not read the Proposals list: {exc}") from exc
    last_seen = last_seen or {}

    policy = await policy_service.for_team(session, team.id if team else None)

    # People this app knows, keyed by email — the only identifier both systems
    # agree on. Display names collide and change; lookup ids are site-local.
    users = list(
        (await session.scalars(select(User).where(User.is_active.is_(True)))).all()
    )
    by_email = {u.email.casefold(): u for u in users if u.email}

    members: set[uuid.UUID] | None = None
    if team is not None:
        members = set(
            (
                await session.scalars(
                    select(TeamMembership.user_id).where(TeamMembership.team_id == team.id)
                )
            ).all()
        )

    rows = workload.get("people", [])
    matched: list[tuple[dict, User | None]] = []
    skipped: list[str] = []

    for row in rows:
        email = (row.get("email") or "").casefold()
        user = by_email.get(email) if email else None
        if members is not None and (user is None or user.id not in members):
            # Scoped to a team, so somebody outside it is not a candidate — but
            # it is recorded, because a silently shorter list is a bug report.
            skipped.append(row.get("name") or "(unnamed)")
            continue
        matched.append((row, user))

    # A team member with no proposal work at all is still a candidate — in fact
    # they are the *best* candidate, and leaving them out would mean the person
    # with nothing to do never gets anything.
    if members is not None:
        seen = {u.id for _, u in matched if u is not None}
        for user in users:
            if user.id in members and user.id not in seen:
                matched.append(({"name": user.display_name, "email": user.email}, user))

    # Roles held globally and inside this team, so a manager can be kept out of
    # the ranking. Fetched once for everybody rather than per person.
    from app.roles.service import global_role_keys

    excluded_roles = {str(r).casefold() for r in (policy.excluded_roles or [])}
    roles_of: dict[uuid.UUID, set[str]] = {}
    if excluded_roles:
        for _, user in matched:
            if user is None:
                continue
            held_roles = await global_role_keys(session, user.id)
            if team is not None:
                held_roles |= await policy_service.team_roles_of(
                    session, team_id=team.id, user_id=user.id
                )
            roles_of[user.id] = held_roles

    scored_users = [u for _, u in matched if u is not None]
    held = await labels_service.effective_labels(
        session,
        scored_users,
        team_id=team.id if team else None,
        new_joiner_days=policy.new_joiner_days,
        new_joiner_from_first_seen=policy.new_joiner_from_first_seen,
    )

    now = datetime.now(UTC)
    candidates: list[Candidate] = []
    for row, user in matched:
        keys = {label.key for label in held.get(user.id, [])} if user else set()
        capacity = policy_service.capacity_for(policy, keys)
        reason = policy_service.is_excluded(policy, keys)
        if reason is None and policy.exclude_on_leave and "on-leave" in keys:
            reason = "On approved leave today"

        # Managers run the queue rather than stand in it.
        if reason is None and user is not None and (
            clash := roles_of.get(user.id, set()) & excluded_roles
        ):
            reason = f"Not given work: holds the {sorted(clash)[0]!r} role"

        open_tasks = int(row.get("open", 0))
        active_tasks = int(row.get("active", 0))
        ceiling = policy_service.max_open_for(policy, keys)
        # Judged on active work: a ceiling that counted closed bids would
        # lock somebody out over rows nobody can act on any more.
        if reason is None and ceiling is not None and active_tasks >= ceiling:
            reason = f"Already holds {active_tasks} active, at the limit of {ceiling}"

        lookup_id = row.get("lookup_id")
        candidates.append(
            Candidate(
                key=str(user.id) if user else f"sp:{lookup_id}",
                display_name=row.get("name") or (user.display_name if user else "Unknown"),
                user_id=str(user.id) if user else None,
                email=row.get("email") or (user.email if user else None),
                lookup_id=lookup_id,
                total_tasks=int(row.get("total", 0)),
                open_tasks=open_tasks,
                completed_tasks=int(row.get("completed", 0)),
                overdue_tasks=int(row.get("overdue", 0)),
                due_soon_tasks=int(row.get("due_soon", 0)),
                no_status_tasks=int(row.get("no_status", 0)),
                expired_tasks=int(row.get("expired", 0)),
                bid_closed_tasks=int(row.get("bid_closed", 0)),
                active_tasks=int(row.get("active", 0)),
                last_assigned_at=last_seen.get(lookup_id) if lookup_id else None,
                labels=keys,
                capacity=capacity,
                max_open=ceiling,
                excluded_reason=reason,
            )
        )

    context = {
        "rows_read": int(workload.get("organisation", {}).get("total", 0)),
        "skipped": skipped,
        "now": now,
    }
    return candidates, policy, context


def weights_of(policy: AssignmentPolicy) -> dict[str, Decimal]:
    return {
        "load_vs_capacity": Decimal(str(policy.weight_load)),
        "open_task_count": Decimal(str(policy.weight_open_count)),
        "days_since_last_assign": Decimal(str(policy.weight_idle_days)),
    }


def snapshot_of(policy: AssignmentPolicy) -> dict[str, Any]:
    """The policy as it stood, frozen onto the run.

    Kept by value as well as by id: a policy edited next week would otherwise
    silently rewrite what this run appears to have been based on.
    """
    return {
        "name": policy.name,
        "team_id": str(policy.team_id) if policy.team_id else None,
        "default_capacity": float(policy.default_capacity),
        "capacity_by_label": dict(policy.capacity_by_label or {}),
        "default_max_open": policy.default_max_open,
        "max_open_by_label": dict(policy.max_open_by_label or {}),
        "excluded_labels": list(policy.excluded_labels or []),
        "excluded_roles": list(policy.excluded_roles or []),
        "exclude_on_leave": policy.exclude_on_leave,
        "new_joiner_days": policy.new_joiner_days,
        "new_joiner_from_first_seen": policy.new_joiner_from_first_seen,
        "weights": {
            "load_vs_capacity": float(policy.weight_load),
            "open_task_count": float(policy.weight_open_count),
            "days_since_last_assign": float(policy.weight_idle_days),
        },
    }


async def in_scope(session: AsyncSession, team: Team) -> AssignmentPolicy:
    """The team's own policy, or a refusal explaining that it has none.

    **A team distributes work through this only if it has an assignment policy
    of its own.** That is what keeps the scoring to presales for now, without
    a team name being written into the code: give another team a policy and it
    is in scope; take it away and it is not.

    The organisation default deliberately does not qualify. It exists as the
    template a new team policy is seeded from, and as the answer to "what would
    apply here" — treating it as a licence to assign would put every team in the
    company into the ranking the moment somebody looked.
    """
    own = await policy_service.own_policy(session, team.id)
    if own is None or not own.enabled:
        raise NotInScopeError(
            f"{team.name} does not distribute work through the assignment scoring. "
            f"Only teams with an assignment policy of their own do. A super admin, "
            f"the CEO or a manager of that team can create one at "
            f"POST /api/v1/assignment/policies/team/{team.id}."
        )
    return own


async def run(
    session: AsyncSession,
    sharepoint: SharePointProposals,
    cache: WorkloadCache,
    *,
    team: Team | None = None,
    refresh: bool = False,
    save: bool = False,
    actor: User | None = None,
    notes: str | None = None,
) -> AnalyticsRun:
    """Compute the ranking, and keep it only if asked.

    ``save=False`` is the default because previewing is the common case, and a
    history full of rankings nobody acted on would make the ones that mattered
    impossible to find.
    """
    candidates, policy, context = await gather(
        session, sharepoint, cache, team=team, refresh=refresh
    )
    if not candidates:
        raise AnalyticsError(
            "Nobody to score. "
            + (
                f"{team.name} has no members with proposal work."
                if team
                else "The Proposals list has no assigned rows."
            )
        )

    results = score(candidates, weights=weights_of(policy), now=context["now"])

    skipped = context["skipped"]
    record = AnalyticsRun(
        team_id=team.id if team else None,
        team_name=team.name if team else None,
        policy_id=policy.id,
        policy_snapshot=snapshot_of(policy),
        source="sharepoint:proposals",
        rows_read=context["rows_read"],
        excluded_note=(
            f"{len(skipped)} people in the Proposals list are not members of "
            f"{team.name}: {', '.join(sorted(skipped)[:12])}"
            if skipped
            else None
        ),
        saved=save,
        notes=notes,
        # The object, not the id. A preview run is never added to the session,
        # so an id alone leaves ``created_by`` unresolvable and the run comes
        # back with no author at all.
        created_by=actor,
    )
    record.entries = [_entry(result, now=context["now"]) for result in results]

    if save:
        session.add(record)
        await session.flush()
        await session.refresh(record)
    else:
        # A preview is never added to the session, so the id and timestamps —
        # which Postgres generates on insert — would still be None, and the
        # response model would refuse them. Filling them in here keeps a preview
        # and a kept run exactly the same shape, so a caller never has to branch
        # on which one they are holding.
        record.id = uuid.uuid4()
        record.created_at = context["now"]
        record.updated_at = context["now"]
        for entry in record.entries:
            entry.id = uuid.uuid4()
            entry.created_at = context["now"]
            entry.updated_at = context["now"]
    return record


def _entry(result: Scored, *, now: datetime) -> UserAnalytics:
    c = result.candidate
    days = (
        None
        if c.last_assigned_at is None
        else int(c.days_since_last_assign(now))
    )
    return UserAnalytics(
        user_id=uuid.UUID(c.user_id) if c.user_id else None,
        display_name=c.display_name,
        email=c.email,
        sharepoint_lookup_id=c.lookup_id,
        total_tasks=c.total_tasks,
        open_tasks=c.open_tasks,
        completed_tasks=c.completed_tasks,
        overdue_tasks=c.overdue_tasks,
        due_soon_tasks=c.due_soon_tasks,
        no_status_tasks=c.no_status_tasks,
        expired_tasks=c.expired_tasks,
        bid_closed_tasks=c.bid_closed_tasks,
        active_tasks=c.active_tasks,
        last_assigned_on=c.last_assigned_at,
        days_since_last_assign=days,
        labels=sorted(c.labels),
        capacity=c.capacity,
        effective_load=c.effective_load,
        max_open=c.max_open,
        excluded=result.excluded,
        excluded_reason=result.excluded_reason,
        priority_score=result.priority,
        factor_total=result.factor_total,
        factors=result.factors,
    )


# ── history ────────────────────────────────────────────────────────────


async def saved_runs(
    session: AsyncSession, *, team_id: uuid.UUID | None = None, limit: int = 50
) -> list[AnalyticsRun]:
    query = (
        select(AnalyticsRun)
        .where(AnalyticsRun.saved.is_(True))
        .order_by(AnalyticsRun.created_at.desc())
        .limit(limit)
    )
    if team_id is not None:
        query = query.where(AnalyticsRun.team_id == team_id)
    return list((await session.scalars(query)).all())


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> AnalyticsRun:
    record = await session.get(AnalyticsRun, run_id)
    if record is None:
        raise AnalyticsNotFoundError("No such analytics run")
    return record


async def delete_run(session: AsyncSession, record: AnalyticsRun) -> None:
    await session.delete(record)
    await session.flush()
