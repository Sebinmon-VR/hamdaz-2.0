from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ReminderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: str
    task_title: str
    task_url: str | None
    end_user: str | None
    due_at: datetime
    status_at_ask: str | None
    submission_at_ask: str | None
    remarks_at_ask: str | None
    working_notes_at_ask: str | None
    team_id: uuid.UUID | None
    assignee_id: uuid.UUID
    assignee_email: str
    assignee_name: str | None = None
    asked_at: datetime | None
    ask_error: str | None
    #: pending, answered or closed.
    status: str
    answered_at: datetime | None
    #: What the answer changed, by SharePoint column name.
    changes: dict[str, str]
    written_at: datetime | None
    write_error: str | None
    closed_note: str | None
    created_at: datetime
    #: The viewer is the person asked, and it still wants an answer.
    may_answer: bool = False


class LiveTaskOut(BaseModel):
    """The four columns as the list has them now, and the choices offered."""

    status: str
    submission_status: str
    remarks: str
    working_notes: str
    status_choices: list[str]
    submission_choices: list[str]
    #: Whether an answer is written to the list now, or only kept here.
    writes_to_sharepoint: bool


class ReminderFormOut(BaseModel):
    reminder: ReminderOut
    #: Null when the list could not be read; the form says so.
    task: LiveTaskOut | None
    task_error: str | None = None


class TaskFields(BaseModel):
    status: str | None = Field(default=None, max_length=80)
    submission_status: str | None = Field(default=None, max_length=80)
    remarks: str | None = Field(default=None, max_length=10_000)
    working_notes: str | None = Field(default=None, max_length=10_000)


class AnswerIn(BaseModel):
    #: What the person wants each column to say. A field left out is not touched.
    values: TaskFields
    #: What the form showed when it opened — so a change made on the list in
    #: the meantime is refused rather than overwritten.
    seen: TaskFields


class TryIn(BaseModel):
    #: The SharePoint list item id of one of the caller's own tasks.
    task_id: str = Field(min_length=1, max_length=64)


class ReminderSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    ask_time: str
    days_before: int
    write_sharepoint: bool
    only_emails: list[str]
    only_title_contains: str
    last_run_on: date | None
    last_run_at: datetime | None
    last_error: str | None
    #: From the overdue follow-up's settings, which decide who is watched.
    team_name: str | None = None
    timezone: str | None = None
    test_mail_to: str | None = None


class ReminderSettingsIn(BaseModel):
    enabled: bool | None = None
    ask_time: str | None = Field(default=None, max_length=5)
    days_before: int | None = None
    write_sharepoint: bool | None = None
    only_emails: list[str] | None = None
    only_title_contains: str | None = Field(default=None, max_length=200)


class RunOut(BaseModel):
    ran: bool
    people: int
    tasks_read: int
    asked: int
    closed: int
    errors: list[str]
