"""Request and response shapes for the teams endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    archived_at: datetime | None
    created_at: datetime
    created_by_id: uuid.UUID | None
    #: Distinct people in the team, not membership rows — someone who is both
    #: lead and approver counts once.
    member_count: int = 0


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    #: Optional. Derived from the name when omitted, de-duplicated if needed.
    slug: str | None = Field(default=None, max_length=64)


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    slug: str | None = Field(default=None, max_length=64)


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    entra_object_id: str
    is_active: bool
    #: Team-scoped roles only. Someone can hold several here.
    role_keys: list[str]
    joined_at: datetime


class TeamDetailOut(TeamOut):
    members: list[MemberOut]


class MemberUpsert(BaseModel):
    """Add someone, or replace the roles they already hold in the team."""

    #: A local user id or an Entra object id. Someone who has never signed in is
    #: provisioned from the directory automatically.
    user_id: str
    #: Empty means the default, ``member``.
    role_keys: list[str] = Field(default_factory=list)


class MemberRoles(BaseModel):
    role_keys: list[str] = Field(default_factory=list)


class BulkAdd(BaseModel):
    """Add several people at once, all with the same roles."""

    user_ids: list[str] = Field(min_length=1)
    role_keys: list[str] = Field(default_factory=list)


class BulkResult(BaseModel):
    added: list[MemberOut]
    #: Anyone who could not be added, with the reason. A partial failure must
    #: not silently look like success.
    failed: list[dict[str, str]]


class MyTeamOut(BaseModel):
    team: TeamOut
    role_keys: list[str]
