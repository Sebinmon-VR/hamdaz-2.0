"""Writing the audit trail.

Root cause #8: the legacy system cannot answer "who approved this quote?" because it has no
action log anywhere. Every mutating operation in Hamdaz 2.0 goes through here.

Two rules that matter:

* **Append-only.** Nothing in application code updates or deletes an audit row.
* **Redacted.** Snapshots pass through :func:`redact` first, so a token or secret that
  happens to live on a model never lands in a log a hundred people can read.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_correlation_id
from app.core.principal import Principal
from app.models.platform import AuditLog

#: Substring match, case-insensitive. Deliberately broad — a false positive costs a redacted
#: field in a log; a false negative leaks a credential.
SENSITIVE_HINTS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "cookie",
    "private_key",
)

REDACTED = "***redacted***"


def redact(value: Any) -> Any:
    """Recursively replace values whose key looks sensitive."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_sensitive(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in SENSITIVE_HINTS)


def _jsonable(value: Any) -> Any:
    """Coerce ORM-ish values into something JSONB will accept."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Only the keys that actually changed.

    Storing whole snapshots makes the audit explorer unreadable; storing the delta makes
    "what changed?" answerable at a glance.
    """
    before = before or {}
    after = after or {}
    changed: dict[str, Any] = {}
    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        if old != new:
            changed[key] = {"from": old, "to": new}
    return changed


async def record(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str | uuid.UUID | None = None,
    actor: Principal | None = None,
    actor_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    """Append one audit row. Flushed, not committed — it joins the caller's transaction.

    That matters: if the operation rolls back, so does its audit entry. An audit log
    recording things that did not happen is worse than none.
    """
    entry = AuditLog(
        actor_id=actor.user_id if actor is not None else actor_id,
        team_id=team_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        before=_jsonable(redact(before)) if before is not None else None,
        after=_jsonable(redact(after)) if after is not None else None,
        ip=ip,
        user_agent=user_agent,
        correlation_id=get_correlation_id(),
        created_at=datetime.now(UTC),
    )
    session.add(entry)
    await session.flush()
    return entry


def snapshot(obj: object, fields: tuple[str, ...]) -> dict[str, Any]:
    """Pull named attributes off a model into a plain dict for before/after capture."""
    return {name: _jsonable(getattr(obj, name, None)) for name in fields}


def build_query(
    *,
    actor_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    correlation_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Select[tuple[AuditLog]]:
    """The audit explorer's filter, shared by the admin and developer panels."""
    query = select(AuditLog).order_by(AuditLog.created_at.desc())

    if actor_id is not None:
        query = query.where(AuditLog.actor_id == actor_id)
    if team_id is not None:
        query = query.where(AuditLog.team_id == team_id)
    if action is not None:
        query = query.where(AuditLog.action == action)
    if entity_type is not None:
        query = query.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(AuditLog.entity_id == entity_id)
    if correlation_id is not None:
        query = query.where(AuditLog.correlation_id == correlation_id)
    if since is not None:
        query = query.where(AuditLog.created_at >= since)
    if until is not None:
        query = query.where(AuditLog.created_at <= until)

    return query
