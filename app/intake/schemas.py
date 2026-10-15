"""What the mirror's API returns: whether the copy is current, and who is next."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class MirrorStatusOut(BaseModel):
    """Whether the local copy of the Proposals list is current."""

    rows: int
    last_sync_at: datetime | None
    rows_read: int
    rows_changed: int
    duration_ms: int
    last_error: str | None
    #: Graph's change subscription on the Proposals list, when one is active.
    #: With it a row edited in SharePoint reaches the ranking in seconds; without
    #: it, at the next tick of the timer.
    subscription_id: str | None = None
    subscription_expires_at: datetime | None = None
    #: Whether the live standing is written to the useranalytics list.
    publish_enabled: bool = False
    publish_list_url: str | None = None


class StandingOut(BaseModel):
    """One person's place in the queue for the next task."""

    user_id: uuid.UUID
    display_name: str
    email: str | None
    #: 1 is next. 0 means they are not in the queue at all.
    rank: int
    eligible: bool
    excluded_reason: str | None
    open_tasks: int
    active_tasks: int
    overdue_tasks: int
    total_tasks: int
    days_since_assigned: int | None
    #: The breakdown behind the position, because a number nobody can
    #: decompose is a number nobody will trust.
    factors: dict[str, Any]
    computed_at: datetime
    reason: str | None
