"""What the intake API accepts and returns."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IntakeSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    mailbox: str
    allowed_senders: list[str]
    allowed_domains: list[str]
    #: **The live-write switch.** On, a new tender becomes a real row in the
    #: Proposals list assigned to a real person. Off — how it ships — the row
    #: that would be created is recorded and nothing is posted.
    create_in_sharepoint: bool
    #: The second write: mark the matched task's Negotiation column, which
    #: is what a Power Automate flow watching the list triggers on.
    update_negotiation: bool
    negotiation_value: str
    #: The third write: set the matched task's OrderStatus column when a
    #: purchase order arrives. Its own switch; ships off.
    update_order_status: bool
    order_status_value: str
    assign_team_id: uuid.UUID | None
    match_threshold: float
    classify_threshold: float
    teams_webhook_url: str | None
    notify_in_app: bool
    notify_teams: bool
    poll_seconds: int
    watch_from: datetime | None
    subscription_id: str | None
    subscription_expires_at: datetime | None
    last_poll_at: datetime | None
    last_error: str | None
    updated_at: datetime


class IntakeSettingsIn(BaseModel):
    """Only the fields given change."""

    enabled: bool | None = None
    #: Whose inbox is watched. Changing it starts from now rather than
    #: replaying the mailbox — a fresh watch must not raise a year of tasks.
    mailbox: str | None = Field(default=None, max_length=320)
    #: Empty admits **nobody**, which is the opposite of the usual convention
    #: and is the point: an unconfigured intake must not read everything.
    allowed_senders: list[str] | None = Field(default=None, max_length=50)
    allowed_domains: list[str] | None = Field(default=None, max_length=20)
    create_in_sharepoint: bool | None = None
    update_negotiation: bool | None = None
    negotiation_value: str | None = Field(default=None, max_length=60)
    update_order_status: bool | None = None
    order_status_value: str | None = Field(default=None, max_length=80)
    assign_team_id: uuid.UUID | None = None
    match_threshold: float | None = Field(default=None, ge=0, le=1)
    classify_threshold: float | None = Field(default=None, ge=0, le=1)
    teams_webhook_url: str | None = Field(default=None, max_length=2000)
    notify_in_app: bool | None = None
    notify_teams: bool | None = None
    poll_seconds: int | None = Field(default=None, ge=15, le=3600)


class IntakeMessageOut(BaseModel):
    """One email and everything decided about it."""

    id: uuid.UUID
    received_at: datetime | None
    sender_email: str | None
    sender_name: str | None
    subject: str | None
    web_link: str | None

    status: str
    category: str | None
    is_reopened: bool
    confidence: float | None
    #: The model's own words. The first thing to read when a decision looks wrong.
    reasoning: str | None
    extracted: dict[str, Any]

    matched_item_id: str | None
    match_confidence: float | None
    match_reason: str | None
    #: The shortlist it chose from, scored. Shows whether a wrong match was a
    #: close call or a wild guess.
    candidates: list[Any]

    action: str
    assigned_user_id: uuid.UUID | None
    assigned_name: str | None
    assigned_reason: str | None
    created_item_id: str | None
    #: Exactly what would be posted to SharePoint. Filled whether or not it was
    #: sent — with writing switched off this is the whole output.
    would_create: dict[str, Any] | None
    #: The change to an existing row — setting Negotiation on the matched
    #: task. Filled whether or not it was sent.
    would_update: dict[str, Any] | None
    notified_user_ids: list[str]
    notified_teams: bool

    error: str | None
    processed_at: datetime | None
    cost_usd: float | None


class IntakePage(BaseModel):
    messages: list[IntakeMessageOut]
    total: int
    #: How many in each status, so a screen can say "3 failed" without paging.
    counts: dict[str, int]


class MirrorStatusOut(BaseModel):
    """Whether the local copy of the Proposals list is current."""

    rows: int
    #: How many carry an embedding. Fewer than ``rows`` means the middle stage
    #: of matching is degraded — usually a missing OpenAI key.
    embedded: int
    last_sync_at: datetime | None
    rows_read: int
    rows_changed: int
    #: Watch this one: if it stays near the row count on every sync, the text
    #: hash is not doing its job and the embedding bill is real.
    rows_embedded: int
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
