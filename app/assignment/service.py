"""Reading and editing the assignment policy.

Two things live here and nothing else does: which policy applies to a team, and
who is allowed to change it. No scoring, no candidate ranking — that is the next
piece of work, and keeping it out means the policy can be set up and argued about
before any of it starts moving work around.

**Resolution.** A team uses its own policy when it has one and it is enabled;
otherwise the org-wide default. Falling back rather than merging is deliberate:
a half-inherited policy would mean nobody could answer "what capacity does a new
joiner have here" without knowing which fields were overridden.

**Who may edit.** Super admins and the CEO edit anything. A manager edits only
the teams they belong to — one manager reshaping another team's workload is not
a permission anyone asked for, and it would be invisible to the team it affected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.labels.catalogue import DEFAULT_CAPACITY_BY_LABEL, DEFAULT_EXCLUDED
from app.models.assignment import MIN_CAPACITY, AssignmentPolicy
from app.models.team import Team, TeamMembership
from app.models.user import User

#: Roles that may edit any team's policy, and the org-wide default.
GLOBAL_EDITORS = frozenset({"super_admin", "ceo"})
#: Organisation-wide roles that may edit a policy for a team they belong to.
TEAM_EDITORS = frozenset({"manager"})

#: Roles held *inside* a team that carry the same right for that team alone.
#: This is how somebody manages one team without any reach over the others.
TEAM_MANAGER_ROLES = frozenset({"team_manager"})

#: Nobody holding one of these is given work by the assignment scoring. Managers
#: run the queue rather than stand in it, and a manager quietly appearing at the
#: top of the ranking is how a team ends up wondering why their lead has twelve
#: proposals. Overridable per policy — a working team lead is a real thing.
DEFAULT_EXCLUDED_ROLES = ("manager", "team_manager")


class PolicyError(Exception):
    """A policy operation was refused. Safe to show a user."""


class PolicyNotFoundError(PolicyError):
    pass


@dataclass(frozen=True, slots=True)
class Reach:
    """What one person may do to one policy, and why."""

    may_edit: bool
    reason: str


async def reach(
    session: AsyncSession,
    *,
    user: User,
    roles: set[str],
    team_id: uuid.UUID | None,
) -> Reach:
    if roles & GLOBAL_EDITORS:
        return Reach(True, "Super admins and the CEO may edit any policy.")

    # A team manager may edit that team's policy without any organisation-wide
    # role at all — which is the point of the role existing.
    if team_id is not None:
        held = await team_roles_of(session, team_id=team_id, user_id=user.id)
        if held & TEAM_MANAGER_ROLES:
            return Reach(True, "Team manager of this team.")

    if not roles & TEAM_EDITORS:
        return Reach(
            False,
            "Only a super admin, the CEO or a manager may change how work is shared out.",
        )

    if team_id is None:
        # The default governs teams a manager has nothing to do with.
        return Reach(
            False,
            "The organisation-wide default can only be changed by a super admin or the CEO.",
        )

    member = await session.scalar(
        select(TeamMembership).where(
            TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
        )
    )
    if member is None:
        return Reach(False, "A manager may only change the policy for their own teams.")
    return Reach(True, "Manager of this team.")


async def team_roles_of(
    session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID
) -> set[str]:
    """The roles somebody holds inside one team."""
    from app.teams.service import team_role_keys

    return await team_role_keys(session, team_id=team_id, user_id=user_id)


async def require_edit(
    session: AsyncSession, *, user: User, roles: set[str], team_id: uuid.UUID | None
) -> None:
    allowed = await reach(session, user=user, roles=roles, team_id=team_id)
    if not allowed.may_edit:
        raise PolicyError(allowed.reason)


# ── reading ────────────────────────────────────────────────────────────


async def default_policy(session: AsyncSession) -> AssignmentPolicy:
    """The org-wide policy, created with sensible values on first use."""
    policy = await session.scalar(
        select(AssignmentPolicy).where(AssignmentPolicy.team_id.is_(None))
    )
    if policy is None:
        policy = AssignmentPolicy(
            team_id=None,
            name="Organisation default",
            description=(
                "Applies to every team without a policy of its own. Capacity is a "
                "multiplier: 0.5 means one task for every two."
            ),
            capacity_by_label=dict(DEFAULT_CAPACITY_BY_LABEL),
            excluded_labels=list(DEFAULT_EXCLUDED),
            excluded_roles=list(DEFAULT_EXCLUDED_ROLES),
        )
        session.add(policy)
        await session.flush()
    return policy


async def for_team(session: AsyncSession, team_id: uuid.UUID | None) -> AssignmentPolicy:
    """The policy that actually governs this team.

    Its own if it has an enabled one, otherwise the org default. The caller can
    tell which by checking ``policy.team_id``.
    """
    if team_id is not None:
        own = await session.scalar(
            select(AssignmentPolicy).where(AssignmentPolicy.team_id == team_id)
        )
        if own is not None and own.enabled:
            return own
    return await default_policy(session)


async def own_policy(session: AsyncSession, team_id: uuid.UUID) -> AssignmentPolicy | None:
    """A team's own policy, or ``None`` if it is running on the default."""
    return await session.scalar(
        select(AssignmentPolicy).where(AssignmentPolicy.team_id == team_id)
    )


async def all_policies(session: AsyncSession) -> list[AssignmentPolicy]:
    return list(
        (
            await session.scalars(
                select(AssignmentPolicy).order_by(
                    AssignmentPolicy.team_id.is_(None).desc(), AssignmentPolicy.name
                )
            )
        ).all()
    )


# ── editing ────────────────────────────────────────────────────────────


def _capacity(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise PolicyError(f"{field} must be a number") from exc
    if number < 0:
        raise PolicyError(f"{field} cannot be negative")
    if number > 100:
        # Not a rule of nature, but a capacity of 500 is a typo every time, and
        # it would quietly funnel an entire team's work to one person.
        raise PolicyError(f"{field} of {number} is implausible; the scale is around 1.0")
    return number


def _validate_capacity_map(raw: dict[str, Any] | None, *, known: set[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in (raw or {}).items():
        clean = str(key).strip().casefold()
        if clean not in known:
            raise PolicyError(
                f"There is no label {clean!r}. Create it first, or remove it from the policy."
            )
        out[clean] = float(_capacity(value, f"capacity for {clean!r}"))
    return out


def _validate_limits(raw: dict[str, Any] | None, *, known: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in (raw or {}).items():
        clean = str(key).strip().casefold()
        if clean not in known:
            raise PolicyError(f"There is no label {clean!r}.")
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise PolicyError(f"The limit for {clean!r} must be a whole number") from exc
        if limit < 0:
            raise PolicyError(f"The limit for {clean!r} cannot be negative")
        out[clean] = limit
    return out


async def update(
    session: AsyncSession,
    policy: AssignmentPolicy,
    *,
    actor: User,
    known_labels: set[str],
    **changes: Any,
) -> AssignmentPolicy:
    """Apply an edit, refusing anything that would make the policy incoherent."""
    if "name" in changes and changes["name"]:
        policy.name = str(changes["name"]).strip()[:160]
    if "description" in changes:
        policy.description = changes["description"]
    if changes.get("enabled") is not None:
        policy.enabled = bool(changes["enabled"])

    if (value := changes.get("default_capacity")) is not None:
        policy.default_capacity = _capacity(value, "default capacity")
    if (value := changes.get("capacity_by_label")) is not None:
        policy.capacity_by_label = _validate_capacity_map(value, known=known_labels)
    if "default_max_open" in changes:
        limit = changes["default_max_open"]
        policy.default_max_open = None if limit is None else max(int(limit), 0)
    if (value := changes.get("max_open_by_label")) is not None:
        policy.max_open_by_label = _validate_limits(value, known=known_labels)

    if (value := changes.get("excluded_labels")) is not None:
        unknown = {str(k).strip().casefold() for k in value} - known_labels
        if unknown:
            raise PolicyError(f"No such label: {', '.join(sorted(unknown))}")
        policy.excluded_labels = [str(k).strip().casefold() for k in value]
    if (value := changes.get("excluded_roles")) is not None:
        policy.excluded_roles = [str(k).strip().casefold() for k in value]
    if changes.get("exclude_on_leave") is not None:
        policy.exclude_on_leave = bool(changes["exclude_on_leave"])
    if changes.get("new_joiner_from_first_seen") is not None:
        policy.new_joiner_from_first_seen = bool(changes["new_joiner_from_first_seen"])

    if (value := changes.get("new_joiner_days")) is not None:
        days = int(value)
        if days < 0 or days > 3650:
            raise PolicyError("The new-joiner window must be between 0 and 3650 days")
        policy.new_joiner_days = days

    weights = {
        field: changes[field]
        for field in ("weight_load", "weight_open_count", "weight_idle_days")
        if changes.get(field) is not None
    }
    for field, value in weights.items():
        number = _capacity(value, field.replace("_", " "))
        setattr(policy, field, number)
    if weights:
        total = (
            policy.weight_load + policy.weight_open_count + policy.weight_idle_days
        )
        if total <= 0:
            # All three at zero means no candidate is ever better than another,
            # which is a policy that cannot decide anything.
            raise PolicyError("At least one scoring weight must be above zero")

    policy.updated_by_id = actor.id
    await session.flush()
    return policy


async def create_for_team(
    session: AsyncSession, *, team: Team, actor: User
) -> AssignmentPolicy:
    """Give a team its own policy, seeded from the org default.

    Copied rather than left blank so the first edit is a change to something that
    already works, not a form of empty boxes with no clue what a sane value is.
    """
    if await own_policy(session, team.id) is not None:
        raise PolicyError(f"{team.name} already has its own policy")

    base = await default_policy(session)
    policy = AssignmentPolicy(
        # The objects, not their ids. The response names the team and the last
        # editor, and unloaded relationships make that a lazy SELECT from inside
        # serialisation — a MissingGreenlet rather than a name.
        team=team,
        name=f"{team.name} assignment policy",
        description=f"Overrides the organisation default for {team.name}.",
        default_capacity=base.default_capacity,
        capacity_by_label=dict(base.capacity_by_label),
        default_max_open=base.default_max_open,
        max_open_by_label=dict(base.max_open_by_label),
        excluded_labels=list(base.excluded_labels),
        excluded_roles=list(base.excluded_roles),
        exclude_on_leave=base.exclude_on_leave,
        new_joiner_days=base.new_joiner_days,
        new_joiner_from_first_seen=base.new_joiner_from_first_seen,
        weight_load=base.weight_load,
        weight_open_count=base.weight_open_count,
        weight_idle_days=base.weight_idle_days,
        updated_by=actor,
    )
    session.add(policy)
    await session.flush()
    return policy


async def delete_for_team(session: AsyncSession, policy: AssignmentPolicy) -> None:
    if policy.is_default:
        raise PolicyError("The organisation default cannot be deleted, only edited")
    await session.delete(policy)
    await session.flush()


# ── what the policy means for one person ───────────────────────────────


def capacity_for(policy: AssignmentPolicy, label_keys: set[str]) -> Decimal:
    """This person's capacity multiplier under this policy.

    The **lowest** matching label wins. Someone who is both senior and in
    training should be treated as in training: the reason they have reduced
    capacity is the constraint, not the seniority, and taking the higher value
    would let a label meant to protect somebody be cancelled out by another.
    """
    candidates = [
        Decimal(str(value))
        for key, value in (policy.capacity_by_label or {}).items()
        if key in label_keys
    ]
    if not candidates:
        return Decimal(str(policy.default_capacity))
    return min(candidates)


def max_open_for(policy: AssignmentPolicy, label_keys: set[str]) -> int | None:
    """The hard ceiling on open work, or ``None`` for no ceiling."""
    limits = [
        int(value)
        for key, value in (policy.max_open_by_label or {}).items()
        if key in label_keys
    ]
    if policy.default_max_open is not None:
        limits.append(int(policy.default_max_open))
    return min(limits) if limits else None


def is_excluded(policy: AssignmentPolicy, label_keys: set[str]) -> str | None:
    """Why this person is out of the pool entirely, or ``None``.

    Capacity 0 counts as exclusion as well as a listed label — a policy that
    gives someone no capacity has already said they take no work, and making the
    admin say it twice invites the two settings to disagree.
    """
    for key in policy.excluded_labels or []:
        if key in label_keys:
            return f"Excluded by the {key!r} label"
    if capacity_for(policy, label_keys) <= MIN_CAPACITY:
        return "Capacity is zero under this policy"
    return None
