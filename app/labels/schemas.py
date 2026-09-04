"""Request and response shapes for labels."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.labels import LabelKind, LabelSource


class LabelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    kind: LabelKind
    color: str | None
    description: str | None
    #: Referred to by the assignment policy. Renameable, not deletable.
    is_system: bool
    #: NULL for an org-wide label.
    team_id: uuid.UUID | None
    #: Worked out at read time rather than given out — on-leave and new-joiner.
    derived: bool = False


class LabelIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    kind: LabelKind
    description: str | None = None
    color: str | None = Field(default=None, max_length=16)


class LabelUpdateIn(BaseModel):
    """A patch. The ``key`` is not here on purpose — see ``update_label``."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    color: str | None = Field(default=None, max_length=16)
    #: Only on a label the product did not ship.
    kind: LabelKind | None = None


class HeldLabelOut(BaseModel):
    """A label somebody actually holds right now."""

    key: str
    name: str
    kind: LabelKind
    #: ``derived`` means nobody assigned it — it follows from other data, and
    #: removing it means changing that data instead.
    source: LabelSource
    expires_at: datetime | None = None
    #: Present on a derived label: why it applies today.
    reason: str | None = None


class PersonLabelsOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    email: str
    joined_on: date | None
    labels: list[HeldLabelOut]


class AssignLabelIn(BaseModel):
    user_id: uuid.UUID
    label_key: str
    #: Scope the label to one team. Omit for everywhere.
    team_id: uuid.UUID | None = None
    #: For a label that should lapse on its own — a training period, a
    #: temporary exclusion. Omit for one that stays until removed.
    expires_at: datetime | None = None
    note: str | None = None


class JoinedOnIn(BaseModel):
    """The date the new-joiner rule counts from."""

    joined_on: date | None
