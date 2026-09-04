"""Managing labels, and working out which ones a person actually holds.

The function that matters here is :func:`effective_labels`. A person's labels are
not simply the rows in ``label_assignments``:

* an assignment with an ``expires_at`` in the past no longer counts;
* an assignment scoped to another team does not count when we are considering
  this team's work;
* ``on-leave`` and ``new-joiner`` are not stored at all — they are computed, from
  approved leave and from the joining date.

Deriving those two rather than storing them is the whole reason nothing here
needs a nightly job. Somebody goes on leave and leaves the assignment pool at
midnight; their leave ends and they are back in it, with no row written either
way and no window in which a stale label is still shaping who gets work.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.labels.catalogue import LABELS, SYSTEM_KEYS
from app.models.labels import (
    DERIVED_KEYS,
    LABEL_NEW_JOINER,
    LABEL_ON_LEAVE,
    Label,
    LabelAssignment,
    LabelKind,
    LabelSource,
)
from app.models.leave import LeaveRequest, LeaveStatus
from app.models.user import User


class LabelError(Exception):
    """A label operation was refused. Safe to show a user."""


class LabelNotFoundError(LabelError):
    pass


@dataclass(frozen=True, slots=True)
class HeldLabel:
    """One label a person holds right now, and where it came from."""

    key: str
    name: str
    kind: LabelKind
    source: LabelSource
    #: Only on a stored assignment that lapses.
    expires_at: datetime | None = None
    #: Why a derived label applies, for the person reading the screen.
    reason: str | None = None


# ── the catalogue ──────────────────────────────────────────────────────


async def seed_labels(session: AsyncSession) -> list[Label]:
    """Create the shipped labels. Idempotent; renames by an admin survive."""
    existing = {
        label.key: label
        for label in (await session.scalars(select(Label).where(Label.team_id.is_(None)))).all()
    }
    for spec in LABELS:
        if (label := existing.get(spec.key)) is None:
            label = Label(
                key=spec.key,
                name=spec.name,
                kind=spec.kind,
                color=spec.color,
                description=spec.description,
                is_system=spec.key in SYSTEM_KEYS,
            )
            session.add(label)
            existing[spec.key] = label
        else:
            # Only the flag is re-asserted. Name, colour and description belong
            # to whoever edited them.
            label.is_system = spec.key in SYSTEM_KEYS
    await session.flush()
    return list(existing.values())


async def all_labels(
    session: AsyncSession, *, team_id: uuid.UUID | None = None
) -> list[Label]:
    """Org-wide labels, plus this team's own."""
    query = select(Label).order_by(Label.kind, Label.name)
    if team_id is not None:
        query = query.where(or_(Label.team_id.is_(None), Label.team_id == team_id))
    return list((await session.scalars(query)).all())


async def get_label(session: AsyncSession, key: str, *, team_id: uuid.UUID | None = None) -> Label:
    label = await session.scalar(
        select(Label).where(Label.key == key, Label.team_id == team_id)
    )
    if label is None:
        raise LabelNotFoundError(f"No label {key!r}")
    return label


async def create_label(
    session: AsyncSession,
    *,
    key: str,
    name: str,
    kind: LabelKind,
    description: str | None = None,
    color: str | None = None,
    team_id: uuid.UUID | None = None,
) -> Label:
    key = key.strip().casefold().replace(" ", "-")
    if not key:
        raise LabelError("A label needs a key")
    if key in DERIVED_KEYS:
        raise LabelError(
            f"{key!r} is worked out automatically and cannot be created by hand"
        )
    if await session.scalar(select(Label).where(Label.key == key, Label.team_id == team_id)):
        raise LabelError(f"A label {key!r} already exists here")

    label = Label(
        key=key,
        name=name.strip(),
        kind=kind,
        description=description,
        color=color,
        team_id=team_id,
    )
    session.add(label)
    await session.flush()
    return label


async def update_label(
    session: AsyncSession,
    label: Label,
    *,
    name: str | None = None,
    description: str | None = None,
    color: str | None = None,
    kind: LabelKind | None = None,
) -> Label:
    """Rename or restyle a label.

    The ``key`` is deliberately not editable. It is what the assignment policy,
    every existing assignment and every stored analytics run refer to, so
    changing it would silently detach all of them — a rename that quietly
    unassigns half a team is not a rename. Change the wording here; if the key
    itself is wrong, make a new label and move people across.

    A system label may be renamed but not have its ``kind`` changed: the policy
    reasons about categories and statuses differently.
    """
    if name is not None:
        if not name.strip():
            raise LabelError("A label needs a name")
        label.name = name.strip()[:120]
    if description is not None:
        label.description = description or None
    if color is not None:
        label.color = color or None
    if kind is not None and kind != label.kind:
        if label.is_system:
            raise LabelError(
                f"{label.key!r} is a system label and its kind cannot be changed — "
                f"the assignment policy treats categories and statuses differently."
            )
        label.kind = kind

    await session.flush()
    return label


async def delete_label(session: AsyncSession, label: Label) -> None:
    if label.is_system:
        raise LabelError(
            f"{label.key!r} is referred to by the assignment policy and cannot be "
            f"deleted. Rename it instead, or stop using it in the policy."
        )
    await session.delete(label)
    await session.flush()


# ── who holds what ─────────────────────────────────────────────────────


async def assign(
    session: AsyncSession,
    *,
    user: User,
    label: Label,
    actor: User | None = None,
    team_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    note: str | None = None,
) -> LabelAssignment:
    if label.key in DERIVED_KEYS:
        raise LabelError(
            f"{label.key!r} is worked out from other data and cannot be given out by hand"
        )

    existing = await session.scalar(
        select(LabelAssignment).where(
            LabelAssignment.user_id == user.id,
            LabelAssignment.label_id == label.id,
            LabelAssignment.team_id == team_id,
        )
    )
    if existing is not None:
        # Re-assigning is how an expiry is extended, so this updates rather than
        # refuses — refusing would make "give them another month" a delete first.
        existing.expires_at = expires_at
        existing.note = note
        existing.assigned_by_id = actor.id if actor else None
        await session.flush()
        return existing

    assignment = LabelAssignment(
        user_id=user.id,
        label_id=label.id,
        team_id=team_id,
        assigned_by_id=actor.id if actor else None,
        expires_at=expires_at,
        note=note,
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def unassign(
    session: AsyncSession, *, user: User, label: Label, team_id: uuid.UUID | None = None
) -> None:
    assignment = await session.scalar(
        select(LabelAssignment).where(
            LabelAssignment.user_id == user.id,
            LabelAssignment.label_id == label.id,
            LabelAssignment.team_id == team_id,
        )
    )
    if assignment is None:
        raise LabelNotFoundError("That person does not hold that label here")
    await session.delete(assignment)
    await session.flush()


# ── the derived ones ───────────────────────────────────────────────────


async def on_leave_today(
    session: AsyncSession, user_ids: list[uuid.UUID], *, on: date | None = None
) -> dict[uuid.UUID, str]:
    """Who is away, and until when. Read live rather than stored.

    Approved leave only: a pending request has not taken anyone out of the pool
    yet, and treating it as though it had would quietly reassign work on the
    strength of a request that might be refused.
    """
    if not user_ids:
        return {}
    day = on or date.today()

    rows = await session.scalars(
        select(LeaveRequest).where(
            LeaveRequest.user_id.in_(user_ids),
            LeaveRequest.status == LeaveStatus.APPROVED,
            LeaveRequest.start_date <= day,
            LeaveRequest.end_date >= day,
        )
    )
    return {
        row.user_id: f"On approved leave until {row.end_date.isoformat()}"
        for row in rows.all()
    }


def is_new_joiner(
    user: User,
    *,
    window_days: int,
    on: date | None = None,
    from_first_seen: bool = False,
) -> str | None:
    """Why this person counts as a new joiner today, or ``None``.

    Uses ``joined_on``. It will fall back to ``created_at`` — first seen in this
    system — but only when ``from_first_seen`` is set, and that is off by
    default for a reason worth stating: with no joining dates recorded, everyone
    first appeared when the ERP was installed, so every single person comes out
    a new joiner and every capacity collapses to the same reduced value. A rule
    that applies to everybody distinguishes nobody. Absence of evidence should
    not halve somebody's workload.
    """
    if window_days <= 0:
        return None

    joined, source = user.joined_on, "joining date"
    if joined is None and from_first_seen and user.created_at is not None:
        joined, source = user.created_at.date(), "first seen in this system"
    if joined is None:
        return None

    day = on or date.today()
    ends = joined + timedelta(days=window_days)
    if day >= ends:
        return None
    return f"Joined {joined.isoformat()} ({source}); new joiner until {ends.isoformat()}"


async def effective_labels(
    session: AsyncSession,
    users: list[User],
    *,
    team_id: uuid.UUID | None = None,
    new_joiner_days: int = 90,
    new_joiner_from_first_seen: bool = False,
    on: date | None = None,
) -> dict[uuid.UUID, list[HeldLabel]]:
    """Every label each person actually holds right now.

    Batched over a list rather than offered per user: the assignment screen needs
    this for a whole team at once, and doing it one at a time would be a query
    per person plus a leave lookup per person.
    """
    if not users:
        return {}

    ids = [u.id for u in users]
    now = datetime.now(UTC)

    stored = await session.scalars(
        select(LabelAssignment).where(
            LabelAssignment.user_id.in_(ids),
            # A label scoped to another team says nothing about this one.
            or_(
                LabelAssignment.team_id.is_(None),
                LabelAssignment.team_id == team_id,
            ),
        )
    )

    held: dict[uuid.UUID, list[HeldLabel]] = {u.id: [] for u in users}
    for assignment in stored.all():
        if not assignment.is_active(now=now):
            continue  # lapsed, so it simply does not apply
        held[assignment.user_id].append(
            HeldLabel(
                key=assignment.label.key,
                name=assignment.label.name,
                kind=assignment.label.kind,
                source=LabelSource.MANUAL,
                expires_at=assignment.expires_at,
            )
        )

    away = await on_leave_today(session, ids, on=on)
    catalogue = {label.key: label for label in await all_labels(session, team_id=team_id)}

    for user in users:
        if reason := away.get(user.id):
            held[user.id].append(_derived(catalogue, LABEL_ON_LEAVE, "On Leave", reason))
        if reason := is_new_joiner(
            user,
            window_days=new_joiner_days,
            on=on,
            from_first_seen=new_joiner_from_first_seen,
        ):
            held[user.id].append(
                _derived(catalogue, LABEL_NEW_JOINER, "New Joiner", reason)
            )

    return held


def _derived(
    catalogue: dict[str, Label], key: str, fallback_name: str, reason: str
) -> HeldLabel:
    label = catalogue.get(key)
    return HeldLabel(
        key=key,
        name=label.name if label else fallback_name,
        kind=label.kind if label else LabelKind.STATUS,
        source=LabelSource.DERIVED,
        reason=reason,
    )
