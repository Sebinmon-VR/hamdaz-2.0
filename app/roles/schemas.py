"""Request and response shapes for the roles endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.role import RoleScope


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    name: str
    description: str | None
    scope: RoleScope
    #: System roles cannot be deleted; the UI should not offer the option.
    is_system: bool


class RoleCreate(BaseModel):
    key: str = Field(min_length=2, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=80)
    scope: RoleScope
    description: str | None = None


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = None


class GrantOut(BaseModel):
    """One global role held by a user."""

    role: RoleOut
    granted_at: datetime
    granted_by_id: uuid.UUID | None


class UserRolesOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    roles: list[GrantOut]
    #: Convenience for the caller: the same thing as bare keys.
    role_keys: list[str]


class MyRolesOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role_keys: list[str]
    #: Whether this caller may manage teams, roles and people.
    is_admin: bool
    is_super_admin: bool


class AssignRole(BaseModel):
    role_key: str
