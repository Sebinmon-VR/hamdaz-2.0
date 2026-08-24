"""User category labels (§5.3).

Labels are the vocabulary the rules engine speaks in. Keeping them separate from roles is the
point: a Senior and a New Joiner can hold identical permissions and still get very different
workloads.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.principal import Principal
from app.models.labels import (
    LABEL_EXCLUDED_FROM_ROTATION,
    LABEL_NEW_JOINER,
    Label,
    LabelAssignment,
    LabelKind,
)
from app.models.platform import AuditAction
from app.services import audit_service

_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Seeded on first run. Admins add whatever else their teams actually use.
DEFAULT_LABELS: tuple[dict[str, str], ...] = (
    {"key": LABEL_NEW_JOINER, "name": "New Joiner", "kind": LabelKind.CATEGORY,
     "description": "Recently joined; carries a reduced workload."},
    {"key": "senior", "name": "Senior", "kind": LabelKind.CATEGORY,
     "description": "Full workload."},
    {"key": "mid", "name": "Mid", "kind": LabelKind.CATEGORY, "description": "Full workload."},
    {"key": "junior", "name": "Junior", "kind": LabelKind.CATEGORY,
     "description": "Reduced workload."},
    {"key": "team-lead", "name": "Team Lead", "kind": LabelKind.CATEGORY,
     "description": "Reduced workload; carries management duties."},
    {"key": "part-time", "name": "Part-time", "kind": LabelKind.CATEGORY,
     "description": "Reduced workload."},
    {"key": "on-probation", "name": "On Probation", "kind": LabelKind.STATUS,
     "description": "Heavily reduced workload."},
    {"key": LABEL_EXCLUDED_FROM_ROTATION, "name": "Excluded from Rotation",
     "kind": LabelKind.STATUS,
     "description": "Never receives automatic assignments. Replaces the legacy exclude list."},
    {"key": "on-notice", "name": "On Notice", "kind": LabelKind.STATUS,
     "description": "Excluded from new assignments."},
)


async def list_labels(
    session: AsyncSession, *, team_id: uuid.UUID | None = None, kind: LabelKind | None = None
) -> list[Label]:
    """Labels visible to a team: its own, plus every org-wide one."""
    query = select(Label)
    if team_id is not None:
        query = query.where(or_(Label.team_id.is_(None), Label.team_id == team_id))
    else:
        query = query.where(Label.team_id.is_(None))
    if kind is not None:
        query = query.where(Label.kind == kind)
    return list((await session.scalars(query.order_by(Label.kind, Label.name))).all())


async def get_label_by_key(
    session: AsyncSession, key: str, *, team_id: uuid.UUID | None = None
) -> Label | None:
    """Team-scoped definition wins over the org-wide one of the same key."""
    if team_id is not None:
        scoped: Label | None = await session.scalar(
            select(Label).where(Label.key == key, Label.team_id == team_id)
        )
        if scoped is not None:
            return scoped
    org_wide: Label | None = await session.scalar(
        select(Label).where(Label.key == key, Label.team_id.is_(None))
    )
    return org_wide


async def create_label(
    session: AsyncSession,
    *,
    actor: Principal,
    key: str,
    name: str,
    kind: LabelKind,
    description: str | None = None,
    color: str | None = None,
    team_id: uuid.UUID | None = None,
) -> Label:
    normalised = key.strip().lower()
    if not _KEY_RE.match(normalised):
        raise ValidationError(
            "A label key must be lowercase words separated by single hyphens, e.g. new-joiner."
        )

    if await session.scalar(
        select(Label).where(Label.key == normalised, Label.team_id.is_(team_id))
    ):
        raise ConflictError(f"A label with the key {normalised!r} already exists here.")

    label = Label(
        key=normalised,
        name=name.strip(),
        kind=kind,
        description=description,
        color=color,
        team_id=team_id,
    )
    session.add(label)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="label",
        entity_id=label.id,
        actor=actor,
        team_id=team_id,
        after={"key": label.key, "name": label.name, "kind": label.kind},
    )
    return label


async def delete_label(
    session: AsyncSession, *, actor: Principal, label_id: uuid.UUID
) -> None:
    label = await session.scalar(select(Label).where(Label.id == label_id))
    if label is None:
        raise NotFoundError("That label does not exist.")

    if label.key in {LABEL_EXCLUDED_FROM_ROTATION, LABEL_NEW_JOINER}:
        raise ValidationError(
            f"{label.name!r} is used by the assignment engine and cannot be deleted."
        )

    holders = await session.scalar(
        select(func.count())
        .select_from(LabelAssignment)
        .where(LabelAssignment.label_id == label_id)
    )
    if holders:
        raise ConflictError(
            f"{holders} user(s) still hold this label. Remove it from them first."
        )

    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        entity_type="label",
        entity_id=label.id,
        actor=actor,
        before={"key": label.key, "name": label.name},
    )
    await session.delete(label)
    await session.flush()


# ── assignments ────────────────────────────────────────────────────────


async def assign_label(
    session: AsyncSession,
    *,
    actor: Principal | None,
    user_id: uuid.UUID,
    label_key: str,
    team_id: uuid.UUID | None = None,
    expires_in_days: int | None = None,
    granted_by_rule_id: uuid.UUID | None = None,
) -> LabelAssignment:
    """Grant a label. Re-granting an existing one refreshes its expiry rather than duplicating.

    That idempotence is what lets the ``user.label`` rule run on a schedule without piling up
    rows or flapping someone's capacity.
    """
    label = await get_label_by_key(session, label_key, team_id=team_id)
    if label is None:
        raise NotFoundError(f"There is no label with the key {label_key!r}.")

    expires_at = (
        datetime.now(UTC) + timedelta(days=expires_in_days)
        if expires_in_days is not None
        else None
    )

    existing = await session.scalar(
        select(LabelAssignment).where(
            LabelAssignment.user_id == user_id,
            LabelAssignment.label_id == label.id,
            LabelAssignment.team_id.is_(team_id),
        )
    )
    if existing is not None:
        existing.expires_at = expires_at
        if granted_by_rule_id is not None:
            existing.granted_by_rule_id = granted_by_rule_id
        await session.flush()
        return existing

    assignment = LabelAssignment(
        user_id=user_id,
        label_id=label.id,
        team_id=team_id,
        assigned_by=actor.user_id if actor is not None else None,
        granted_by_rule_id=granted_by_rule_id,
        expires_at=expires_at,
    )
    session.add(assignment)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="label_assignment",
        entity_id=assignment.id,
        actor=actor,
        team_id=team_id,
        after={
            "user_id": str(user_id),
            "label": label.key,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "automatic": granted_by_rule_id is not None,
        },
    )
    return assignment


async def revoke_label(
    session: AsyncSession,
    *,
    actor: Principal | None,
    user_id: uuid.UUID,
    label_key: str,
    team_id: uuid.UUID | None = None,
) -> bool:
    label = await get_label_by_key(session, label_key, team_id=team_id)
    if label is None:
        return False

    assignment = await session.scalar(
        select(LabelAssignment).where(
            LabelAssignment.user_id == user_id,
            LabelAssignment.label_id == label.id,
            LabelAssignment.team_id.is_(team_id),
        )
    )
    if assignment is None:
        return False

    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        entity_type="label_assignment",
        entity_id=assignment.id,
        actor=actor,
        team_id=team_id,
        before={"user_id": str(user_id), "label": label.key},
    )
    await session.delete(assignment)
    await session.flush()
    return True


async def active_labels_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, team_id: uuid.UUID | None = None
) -> frozenset[str]:
    """Unexpired label keys, org-wide plus the given team's."""
    now = datetime.now(UTC)
    rows = (
        await session.scalars(
            select(LabelAssignment)
            .where(LabelAssignment.user_id == user_id)
            .options(selectinload(LabelAssignment.label))
        )
    ).all()

    return frozenset(
        row.label.key
        for row in rows
        if row.label is not None
        and row.is_active(now=now)
        and (row.team_id is None or row.team_id == team_id)
    )


async def purge_expired(session: AsyncSession) -> int:
    """Delete assignments that expired. Housekeeping only — reads already filter by expiry."""
    now = datetime.now(UTC)
    expired = (
        await session.scalars(
            select(LabelAssignment).where(
                LabelAssignment.expires_at.is_not(None), LabelAssignment.expires_at <= now
            )
        )
    ).all()
    for row in expired:
        await session.delete(row)
    await session.flush()
    return len(expired)


async def seed_default_labels(session: AsyncSession) -> int:
    """Create the built-in labels if absent. Idempotent."""
    created = 0
    for spec in DEFAULT_LABELS:
        exists = await session.scalar(
            select(Label).where(Label.key == spec["key"], Label.team_id.is_(None))
        )
        if exists is None:
            session.add(
                Label(
                    key=spec["key"],
                    name=spec["name"],
                    kind=LabelKind(spec["kind"]),
                    description=spec.get("description"),
                )
            )
            created += 1
    await session.flush()
    return created
