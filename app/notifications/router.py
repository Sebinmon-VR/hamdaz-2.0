"""The bell: what this person has been told, and marking it seen.

Everything here is scoped to the caller in the query itself rather than checked
afterwards. There is no route that takes a user id, because there is no reason
for one person to read another's notifications, and the safest way to keep that
true is for the API to have no way to express it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.notifications import service

router = APIRouter(prefix="/notifications", tags=["notifications"])

Session = Annotated[AsyncSession, Depends(get_session)]


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    title: str
    body: str | None
    #: A path within the app rather than an absolute URL, so it survives the
    #: frontend moving.
    link: str | None
    source: str | None
    source_id: str | None
    payload: dict[str, Any]
    read_at: datetime | None
    sent_to_teams: bool
    created_at: datetime


class NotificationPage(BaseModel):
    notifications: list[NotificationOut]
    total: int
    unread: int


class MarkReadIn(BaseModel):
    """Which to mark. Omit ``ids`` to mark everything unread."""

    ids: list[uuid.UUID] | None = Field(default=None, max_length=500)


@router.get("", response_model=NotificationPage, summary="What I have been told")
async def my_notifications(
    user: CurrentUser,
    session: Session,
    unread_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NotificationPage:
    rows, total = await service.listing(
        session, user.id, unread_only=unread_only, limit=limit, offset=offset
    )
    return NotificationPage(
        notifications=[NotificationOut.model_validate(r) for r in rows],
        total=total,
        unread=await service.unread_count(session, user.id),
    )


@router.get("/unread-count", summary="Just the number on the bell")
async def unread(user: CurrentUser, session: Session) -> dict[str, int]:
    """Its own route because a frontend polls this and nothing else.

    Returning the whole list to render a number would be the sort of thing
    that makes a page slow for no reason anybody can point at.
    """
    return {"unread": await service.unread_count(session, user.id)}


@router.post("/read", summary="Mark as read")
async def mark_read(
    body: MarkReadIn, user: CurrentUser, session: Session
) -> dict[str, int]:
    """An id belonging to somebody else simply matches nothing.

    Scoped in the statement rather than checked first: the safe outcome and one
    fewer round trip.
    """
    changed = await service.mark_read(session, user.id, ids=body.ids)
    await session.commit()
    return {"marked": changed, "unread": await service.unread_count(session, user.id)}
