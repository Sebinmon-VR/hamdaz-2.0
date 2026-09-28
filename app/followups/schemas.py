"""What the follow-up API accepts and returns."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FollowupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: str
    task_title: str
    task_url: str | None
    end_user: str | None
    status_at_ask: str | None
    due_at: datetime
    task_modified_at: datetime | None
    team_id: uuid.UUID | None
    team_name: str | None = None
    assignee_id: uuid.UUID
    assignee_email: str
    assignee_name: str | None = None
    asked_at: datetime | None
    ask_error: str | None
    asked_from_email: str | None
    #: pending, answered, false_positive or resolved.
    status: str
    reason: str | None
    answered_at: datetime | None
    forwarded_at: datetime | None
    forward_error: str | None
    resolved_note: str | None
    created_at: datetime
    #: Whether the viewer is the person asked and it still wants an answer.
    may_answer: bool = False


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=5000)


class FalsePositiveIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class FollowupSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    team_id: uuid.UUID | None
    team_name: str | None = None
    only_emails: list[str]
    only_title_contains: str
    grace_minutes: int
    poll_seconds: int
    watch_from: datetime | None
    ask_from_user_id: uuid.UUID | None
    ask_from_email: str | None = None
    notify_managers_by_email: bool
    digest_enabled: bool = True
    #: "HH:MM" on ``digest_timezone``'s clock.
    digest_time: str = "18:00"
    digest_timezone: str = "Asia/Kolkata"
    digest_recipients: list[str] = Field(default_factory=list)
    digest_include_ceo: bool = False
    digest_formats: list[str] = Field(default_factory=lambda: ["pdf", "xlsx"])
    digest_sender_email: str | None = None
    digest_last_sent_on: date | None = None
    digest_last_error: str | None = None
    weekly_enabled: bool = True
    #: 0 Monday … 6 Sunday.
    weekly_day: int = 4
    weekly_last_sent_on: date | None = None
    #: Who it would go to right now, the CEO role holders included.
    digest_to: list[str] = Field(default_factory=list)
    last_run_at: datetime | None
    last_error: str | None
    updated_at: datetime


class FollowupSettingsIn(BaseModel):
    """Only the fields sent change."""

    enabled: bool | None = None
    team_id: uuid.UUID | None = None
    only_emails: list[str] | None = Field(default=None, max_length=50)
    only_title_contains: str | None = Field(default=None, max_length=120)
    grace_minutes: int | None = Field(default=None, ge=0, le=7 * 24 * 60)
    poll_seconds: int | None = Field(default=None, ge=30, le=3600)
    #: Move the watch window by hand — usually back, to include a test task
    #: that fell due just before the feature was switched on.
    watch_from: datetime | None = None
    ask_from_user_id: uuid.UUID | None = None
    notify_managers_by_email: bool | None = None
    digest_enabled: bool | None = None
    digest_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    digest_timezone: str | None = Field(default=None, max_length=64)
    digest_recipients: list[str] | None = Field(default=None, max_length=30)
    digest_include_ceo: bool | None = None
    digest_formats: list[Literal["pdf", "xlsx"]] | None = Field(default=None, min_length=1)
    digest_sender_email: str | None = Field(default=None, max_length=320)
    weekly_enabled: bool | None = None
    weekly_day: int | None = Field(default=None, ge=0, le=6)


class DigestOut(BaseModel):
    day: str
    #: "day" or "week".
    period: str = "day"
    lines: int
    submitted: int = 0
    not_submitted: int = 0
    not_responded: int
    recipients: list[str]
    sent: bool
    error: str | None


class SweepOut(BaseModel):
    people: int
    tasks_read: int
    asked: int
    resolved: int
    errors: list[str]
    settings: dict[str, Any] | None = None


class TryIn(BaseModel):
    #: The SharePoint list item id of one of the caller's own tasks.
    task_id: str = Field(min_length=1, max_length=64)


class DueTodayTaskOut(BaseModel):
    task_id: str
    title: str
    task_url: str | None
    end_user: str | None
    status: str | None
    submission_status: str | None
    #: The bid is marked submitted — what "done" means to the follow-up.
    finished: bool
    #: Marked "Not Submitted" outright, so the reason is asked for now rather
    #: than at the due time.
    reason_now: bool = False
    assignee_name: str
    assignee_email: str
    due_at: datetime
    #: When the "why is it late" question goes out if it is still unfinished.
    ask_at: datetime
    followup_id: uuid.UUID | None
    followup_status: str | None
    #: The question's email went out. False with ``mail_error`` when it did not.
    mailed: bool = False
    mail_error: str | None = None
    #: Why this task will never be asked about under the current settings —
    #: the trial filters, or the follow-up being off. Null when it is watched.
    not_watched: str | None = None


class DueTodayOut(BaseModel):
    team_name: str | None
    #: Whether this is the whole team or only the viewer's own tasks.
    scope: str
    grace_minutes: int
    generated_at: datetime
    tasks: list[DueTodayTaskOut]
