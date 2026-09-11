"""Telling people things, in the app and in Teams.

In-app is the record; Teams is a copy of it. A channel message cannot be marked
read, cannot be listed, and is gone up the channel by the afternoon — so what
happened is stored here first and posted there second. A misconfigured webhook
then loses a duplicate rather than the only trace.

The Teams half uses an **incoming webhook**, a URL created in the channel
itself. That is not the richest integration Microsoft offers, and it is chosen
anyway: sending a chat message to a *person* through application permissions is
heavily restricted and may simply be refused in a given tenant, whereas a
webhook works the afternoon somebody pastes the URL in. The shape here leaves
room for a per-user sender later — ``notify`` already takes users, and only the
delivery would change.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification, NotificationKind
from app.models.user import User

logger = logging.getLogger("hamdaz.notifications")


async def notify(
    session: AsyncSession,
    *,
    users: Sequence[User] | Sequence[uuid.UUID],
    kind: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> list[Notification]:
    """Raise one notification per person, at most once each.

    ``source`` and ``source_id`` together make it idempotent: the same email
    processed twice, or a retried webhook, updates the existing row instead of
    filling somebody's bell with copies. Without a ``source_id`` there is
    nothing to deduplicate on and every call raises a new one, which is right
    for things that genuinely recur.
    """
    ids = [u.id if isinstance(u, User) else u for u in users]
    if not ids or not title.strip():
        return []

    made: list[Notification] = []
    for user_id in dict.fromkeys(ids):
        values = {
            "user_id": user_id,
            "kind": kind,
            "title": title.strip()[:300],
            "body": body,
            "link": link,
            "source": source,
            "source_id": source_id,
            "payload": payload or {},
        }
        if source_id:
            statement = (
                insert(Notification)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[
                        Notification.user_id, Notification.source, Notification.source_id
                    ],
                    index_where=Notification.source_id.is_not(None),
                    set_={"title": values["title"], "body": body, "payload": values["payload"]},
                )
                .returning(Notification.id)
            )
            await session.execute(statement)
        else:
            row = Notification(**values)
            session.add(row)
            made.append(row)
    await session.flush()

    if source_id:
        made = list(
            (
                await session.scalars(
                    select(Notification).where(
                        Notification.source == source,
                        Notification.source_id == source_id,
                        Notification.user_id.in_(ids),
                    )
                )
            ).all()
        )
    return made


async def unread_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
        or 0
    )


async def listing(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Notification], int]:
    """This person's notifications, newest first. Never anybody else's."""
    query = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    total = int(
        await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    )
    rows = (
        await session.scalars(
            query.order_by(Notification.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return list(rows), total


async def mark_read(
    session: AsyncSession, user_id: uuid.UUID, *, ids: Iterable[uuid.UUID] | None = None
) -> int:
    """Mark some, or all, of this person's notifications read.

    Scoped to the caller in the statement itself rather than checked first: an
    id belonging to somebody else simply matches nothing, which is both the
    safe outcome and one fewer round trip.
    """
    statement = (
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    if ids is not None:
        wanted = list(ids)
        if not wanted:
            return 0
        statement = statement.where(Notification.id.in_(wanted))
    result = await session.execute(statement)
    return int(result.rowcount or 0)


# ── the Teams copy ─────────────────────────────────────────────────────


def _card(
    *, title: str, body: str | None, facts: dict[str, Any], link: str | None
) -> dict[str, Any]:
    """An Adaptive Card, which is what a channel webhook renders properly.

    Plain text posts as an unstyled line that scrolls away unread. The facts
    table is the part people actually use — deadline, customer, who holds it —
    so it is built from whatever the caller had rather than being formatted
    into the body text where it cannot be scanned.
    """
    rows = [
        {"title": str(k), "value": str(v)}
        for k, v in facts.items()
        if v not in (None, "", [])
    ]
    content: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}
    ]
    if body:
        content.append({"type": "TextBlock", "text": body, "wrap": True})
    if rows:
        content.append({"type": "FactSet", "facts": rows})

    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": content,
    }
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open", "url": link}]

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


async def send_to_teams(
    http: httpx.AsyncClient,
    webhook_url: str,
    *,
    title: str,
    body: str | None = None,
    facts: dict[str, Any] | None = None,
    link: str | None = None,
) -> bool:
    """Post one card to a Teams channel. True if it went.

    Never raises. A notification is already recorded in the app before this is
    called, so a webhook that is wrong, expired or unreachable costs a copy and
    not the fact — and failing the thing that caused it would be absurd.
    """
    if not webhook_url:
        return False
    try:
        response = await http.post(
            webhook_url, json=_card(title=title, body=body, facts=facts or {}, link=link)
        )
    except Exception as exc:  # noqa: BLE001 - a copy, never the record
        logger.warning("Teams webhook failed: %s", exc)
        return False
    if response.status_code >= 400:
        logger.warning(
            "Teams webhook refused the card (%s): %s",
            response.status_code, response.text[:200],
        )
        return False
    return True


async def notify_and_post(
    session: AsyncSession,
    http: httpx.AsyncClient,
    *,
    users: Sequence[User],
    webhook_url: str | None,
    kind: str = NotificationKind.GENERAL,
    title: str,
    body: str | None = None,
    facts: dict[str, Any] | None = None,
    link: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    in_app: bool = True,
    to_teams: bool = True,
) -> list[Notification]:
    """The usual case: record it, then post a copy to the channel.

    In that order, deliberately. The record is what somebody can come back to;
    the channel post is what gets their attention today.
    """
    made: list[Notification] = []
    if in_app:
        made = await notify(
            session,
            users=users,
            kind=kind,
            title=title,
            body=body,
            link=link,
            source=source,
            source_id=source_id,
            payload=facts or {},
        )
    if to_teams and webhook_url:
        sent = await send_to_teams(
            http, webhook_url, title=title, body=body, facts=facts, link=link
        )
        for row in made:
            row.sent_to_teams = sent
        await session.flush()
    return made
