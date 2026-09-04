"""Request and response shapes for module visibility."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class PageOut(BaseModel):
    key: str
    name: str
    #: The frontend route, so one catalogue drives both permissions and navigation.
    path: str
    #: Rendered inside a team's context; its path carries [slug].
    team_scoped: bool = False


class ModuleOut(BaseModel):
    key: str
    name: str
    description: str
    #: Reached through a global admin role; never granted to a team.
    admin_only: bool
    pages: list[PageOut]


class ModuleGrantOut(BaseModel):
    module_key: str
    name: str
    #: True means the whole module, including pages added to it later.
    all_pages: bool
    pages: list[PageOut]
    granted_at: datetime
    granted_by_id: uuid.UUID | None


class TeamAccessOut(BaseModel):
    team_id: uuid.UUID
    slug: str
    name: str
    modules: list[ModuleGrantOut]


class GrantModule(BaseModel):
    module_key: str
    #: Omit for the whole module; a list pins the grant to exactly those pages.
    page_keys: list[str] | None = None


class SetAccess(BaseModel):
    """The team's complete access, replacing whatever it had."""

    #: module key -> page keys, or null for the whole module.
    modules: dict[str, list[str] | None] = Field(default_factory=dict)


class EffectiveModule(BaseModel):
    key: str
    name: str
    admin_only: bool
    pages: list[PageOut]


class EffectiveAccessOut(BaseModel):
    user_id: uuid.UUID
    #: "super_admin" when everything is visible by role, "teams" otherwise.
    source: str
    modules: list[EffectiveModule]
    #: Which teams the access came from. Empty for a super admin.
    via_teams: list[str]
