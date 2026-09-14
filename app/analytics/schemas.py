"""Response shapes for user analytics and the priority score."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class FactorOut(BaseModel):
    """One factor's part in one person's score."""

    raw: float
    #: 0..1 within this group, where 1 always means "most deserving of the next".
    normalised: float
    weight: float
    contribution: float


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID | None
    display_name: str
    email: str | None
    sharepoint_lookup_id: str | None

    total_tasks: int
    open_tasks: int
    completed_tasks: int
    overdue_tasks: int
    due_soon_tasks: int
    #: Part of open_tasks. A fifth of the Proposals list has no status at all,
    #: and those rows count as open — worth seeing separately before trusting
    #: somebody's load.
    no_status_tasks: int
    #: Read as finished: no status was ever set and the bid closed.
    expired_tasks: int
    #: Not finished, but the bid closed. Not current workload.
    bid_closed_tasks: int
    #: Not finished and the bid is today or later. The score is built on this.
    active_tasks: int

    last_assigned_on: datetime | None
    days_since_last_assign: int | None

    labels: list[str]
    capacity: Decimal
    #: open_tasks / capacity — the number that makes a ratio work.
    effective_load: Decimal
    max_open: int | None

    excluded: bool
    excluded_reason: str | None
    #: **1, 2, 3, 4 ... where 1 is the highest priority** — next in line for
    #: work. Consecutive over the assignable people, no gaps. Null for anyone
    #: excluded: out of the queue is not the same as last in it.
    priority_score: int | None
    #: The sum of the weighted factor contributions, 0..1. **Not a score** — the
    #: score is ``priority_score``. This only shows how far apart two positions
    #: are, since 1 and 2 look identical whether they were neck and neck or not.
    factor_total: Decimal | None
    #: Per factor, so a ranking can be argued with rather than just announced.
    factors: dict[str, FactorOut]


class PublishOut(BaseModel):
    """What one push to the useranalytics list did."""

    enabled: bool
    list_url: str
    #: What triggered it: publish, saved-run, mirror, intake.
    reason: str
    created: int
    updated: int
    #: Rows that already said this. Not written, so ``Modified`` is untouched.
    unchanged: int
    #: Who was written, created and updated together.
    names: list[str]
    error: str | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    team_id: uuid.UUID | None
    team_name: str | None
    #: The policy exactly as it stood, so a later edit cannot rewrite history.
    policy_snapshot: dict[str, Any]
    source: str
    rows_read: int
    excluded_note: str | None
    saved: bool
    notes: str | None
    created_at: datetime
    created_by_name: str | None = None
    entries: list[EntryOut]

    #: Who should get the next task. Null if nobody is assignable.
    next_up: str | None = None
    assignable: int = 0
    #: What reached the useranalytics list when this run was kept. Null when
    #: publishing is off. An error here does not mean the run was not kept.
    published: PublishOut | None = None


class RunSummaryOut(BaseModel):
    """A history row — no entries, so a list of fifty runs stays small."""

    id: uuid.UUID
    team_name: str | None
    rows_read: int
    people: int
    assignable: int
    next_up: str | None
    notes: str | None
    created_at: datetime
    created_by_name: str | None


class RunIn(BaseModel):
    """Keep this run as the record of a decision."""

    notes: str | None = None
