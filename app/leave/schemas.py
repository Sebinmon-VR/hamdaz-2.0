"""Request and response shapes for leave."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.leave import LeaveType


class LeaveRequestIn(BaseModel):
    leave_type: LeaveType = LeaveType.ANNUAL
    #: Both ends inclusive. A single day has start == end.
    start_date: date
    end_date: date
    reason: str | None = Field(default=None, max_length=2000)


class LeaveRequestOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str
    user_email: str
    leave_type: str
    start_date: date
    end_date: date
    days: int
    reason: str | None
    status: str
    #: "system" for an automatic decision, "hr" for a person, "requester" if withdrawn.
    decided_by: str | None
    decided_by_id: uuid.UUID | None
    decided_at: datetime | None
    #: Always present on a rejection.
    decision_note: str | None
    #: Approved by HR despite the concurrent limit.
    emergency_override: bool
    #: How many others were already off on the busiest day, at decision time.
    conflicting_count: int | None
    notified_at: datetime | None
    notify_error: str | None
    created_at: datetime


class DecideIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
    #: Required to approve past the concurrent limit.
    emergency: bool = False


class RejectIn(BaseModel):
    #: Mandatory: a refusal without a reason is the thing people complain about.
    note: str = Field(min_length=1, max_length=2000)


class LeaveSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    max_concurrent: int
    auto_decide: bool
    limit_scope: str
    hr_team_slug: str
    notify_hr_by_email: bool
    max_days_per_request: int


class LeaveSettingsIn(BaseModel):
    #: How many people may be on approved leave on the same day.
    max_concurrent: int | None = Field(default=None, ge=1, le=999)
    #: Off means every request waits for a human.
    auto_decide: bool | None = None
    limit_scope: Literal["organisation", "team"] | None = None
    hr_team_slug: str | None = Field(default=None, max_length=64)
    #: Mail HR from the requester's own mailbox. Off by default.
    notify_hr_by_email: bool | None = None
    max_days_per_request: int | None = Field(default=None, ge=1, le=365)


class CalendarOut(BaseModel):
    start: date
    end: date
    #: ISO date -> the people off that day. Only days with somebody off appear.
    days: dict[str, list[dict[str, Any]]]


class LeaveSummaryOut(BaseModel):
    total: int
    pending: int
    approved: int
    rejected: int
    cancelled: int
    days_approved: int
    upcoming: list[dict[str, Any]]
