"""Request and response shapes for form templates."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.templates import FieldType, TemplateStatus


class FieldIn(BaseModel):
    """One field. A frontend renders from this and nothing else."""

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=160)
    type: FieldType
    section: str | None = Field(default=None, max_length=64)
    required: bool = False
    help: str | None = None
    #: For ``select``.
    options: list[str] | None = None
    default: Any = None
    #: The Zoho estimate field this becomes, where there is one. Absent means
    #: the field is ours alone and is not sent anywhere.
    maps_to: str | None = Field(default=None, max_length=64)
    #: For ``table`` — the columns of a repeating row, as field specs.
    columns: list[dict[str, Any]] | None = None
    #: Makes the field count towards a score. ``{"tags": [...], "max": 5,
    #: "weight": 1.0, "option_scores": {...}}`` — see ``app.forms.scoring``.
    #: Only select, number, percent and checkbox fields may carry one.
    scoring: dict[str, Any] | None = None


class SectionIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    help: str | None = None


class TemplateIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    #: What the form is for, so a module can find its own. Defaults to the key.
    kind: str | None = Field(default=None, max_length=64)
    description: str | None = None
    fields: list[FieldIn] = Field(default_factory=list)
    sections: list[SectionIn] = Field(default_factory=list)


class TemplateUpdateIn(BaseModel):
    """A patch — anything left out is untouched."""

    name: str | None = Field(default=None, max_length=160)
    kind: str | None = Field(default=None, max_length=64)
    description: str | None = None
    fields: list[FieldIn] | None = None
    sections: list[SectionIn] | None = None


class GrantIn(BaseModel):
    """Who may use a template.

    Omit ``team_id`` for every team; leave ``allowed_roles`` empty for anyone on
    the team it covers. Both absences mean "no restriction on that axis".
    """

    team_id: uuid.UUID | None = None
    allowed_roles: list[str] = Field(default_factory=list)
    note: str | None = None


class GrantOut(BaseModel):
    #: from_attributes because TemplateOut validates straight off the ORM row,
    #: and its ``grants`` are TemplateGrant objects. Without it, validation of
    #: the parent fails before any code gets a chance to convert them.
    model_config = ConfigDict(from_attributes=True)

    team_id: uuid.UUID | None
    #: Filled in by the router — the ORM row has a team, not a team name.
    team_name: str | None = None
    allowed_roles: list[str]
    note: str | None = None


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    kind: str
    description: str | None
    status: TemplateStatus
    version: int
    sections: list[dict[str, Any]]
    fields: list[dict[str, Any]]
    created_by_name: str | None = None
    #: Every tag this template can score against, in field order. Empty for a
    #: template that only records answers rather than judging them.
    score_tags: list[str] = Field(default_factory=list)
    grants: list[GrantOut] = Field(default_factory=list)
    #: Whether the caller may fill this in, and why not if they may not.
    may_use: bool = False
    use_reason: str | None = None
    may_edit: bool = False


class TemplateSummaryOut(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    kind: str
    status: TemplateStatus
    version: int
    field_count: int
    grant_count: int
    description: str | None
    #: Whether filling this in produces a score. Lets a list distinguish an
    #: assessment from a plain form without opening either.
    scored: bool = False
