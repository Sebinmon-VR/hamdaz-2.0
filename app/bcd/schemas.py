from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BcdCheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: str
    task_title: str
    task_url: str | None
    task_created_at: datetime | None
    placeholder_bcd: datetime | None
    team_id: uuid.UUID | None
    assignee_id: uuid.UUID
    assignee_email: str
    assignee_name: str | None = None
    #: pending, corrected, confirmed or closed.
    status: str
    asked_at: datetime | None
    ask_error: str | None
    escalated_at: datetime | None
    escalate_error: str | None
    resolved_at: datetime | None
    resolved_by_name: str | None = None
    resolved_note: str | None
    created_at: datetime
    #: The task's SharePoint edit form, where the BCD is corrected.
    edit_url: str | None = None
    #: Whether the viewer may confirm the date as it stands.
    may_confirm: bool = False


class BcdFormOut(BaseModel):
    check: BcdCheckOut
    #: The BCD on the list now, and whether it is still the placeholder.
    current_bcd: str | None = None
    still_placeholder: bool | None = None
    task_error: str | None = None


class BcdSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    work_start: str
    work_end: str
    timezone: str
    work_days: list[int]
    escalate_after_minutes: int
    watch_from: datetime | None
    only_emails: list[str]
    only_title_contains: str
    last_run_at: datetime | None
    last_error: str | None
    #: From the follow-up's settings, which decide who is watched.
    team_name: str | None = None
    team_leads: list[str] = Field(default_factory=list)
    escalate_to: list[str] = Field(default_factory=list)


class BcdSettingsIn(BaseModel):
    enabled: bool | None = None
    work_start: str | None = Field(default=None, max_length=5)
    work_end: str | None = Field(default=None, max_length=5)
    timezone: str | None = Field(default=None, max_length=64)
    work_days: list[int] | None = None
    escalate_after_minutes: int | None = None
    only_emails: list[str] | None = None
    only_title_contains: str | None = Field(default=None, max_length=200)


class BcdRunOut(BaseModel):
    ran: bool
    tasks_read: int
    found: int
    asked: int
    escalated: int
    resolved: int
    errors: list[str]


class BcdTryIn(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
