"""Request and response shapes for the assignment policy."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    #: NULL is the organisation-wide default.
    team_id: uuid.UUID | None
    team_name: str | None = None
    name: str
    description: str | None
    enabled: bool

    default_capacity: Decimal
    #: Label key -> multiplier. 0.5 means one task for every two.
    capacity_by_label: dict[str, float]
    default_max_open: int | None
    max_open_by_label: dict[str, int]
    excluded_labels: list[str]
    #: Roles that are never given work — managers by default.
    excluded_roles: list[str]
    exclude_on_leave: bool
    new_joiner_days: int
    new_joiner_from_first_seen: bool

    weight_load: Decimal
    weight_open_count: Decimal
    weight_idle_days: Decimal

    updated_at: datetime
    updated_by_name: str | None = None
    #: Whether the caller may change this one, and why not if they may not.
    may_edit: bool = False
    edit_reason: str | None = None


class PolicyIn(BaseModel):
    """Every field optional — this is a patch, not a replacement.

    A full replacement would mean a manager editing one capacity had to send
    back every other setting, and would silently reset anything their screen did
    not know about.
    """

    name: str | None = Field(default=None, max_length=160)
    description: str | None = None
    enabled: bool | None = None

    default_capacity: Decimal | None = Field(default=None, ge=0, le=100)
    capacity_by_label: dict[str, float] | None = None
    default_max_open: int | None = Field(default=None, ge=0)
    max_open_by_label: dict[str, int] | None = None
    excluded_labels: list[str] | None = None
    excluded_roles: list[str] | None = None
    exclude_on_leave: bool | None = None
    new_joiner_days: int | None = Field(default=None, ge=0, le=3650)
    new_joiner_from_first_seen: bool | None = None

    weight_load: Decimal | None = Field(default=None, ge=0, le=100)
    weight_open_count: Decimal | None = Field(default=None, ge=0, le=100)
    weight_idle_days: Decimal | None = Field(default=None, ge=0, le=100)


class EffectOut(BaseModel):
    """What the policy means for one person, before any work is scored.

    This is the preview: it says who is in the pool and at what capacity, so the
    ratios can be checked against real people rather than argued about in the
    abstract.
    """

    user_id: uuid.UUID
    display_name: str
    labels: list[str]
    capacity: Decimal
    #: Interpreting the ratio for a reader: 0.5 -> "1 task for every 2".
    ratio: str
    max_open: int | None
    excluded: bool
    #: Why they are out of the pool, when they are.
    excluded_reason: str | None


class PolicyPreviewOut(BaseModel):
    policy_id: uuid.UUID
    #: True when this team is running on the organisation default.
    inherited: bool
    team_id: uuid.UUID | None
    people: list[EffectOut]
    #: How many are in the pool at all.
    assignable: int
