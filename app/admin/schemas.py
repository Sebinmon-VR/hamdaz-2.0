"""What the administration console returns."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class EndpointOut(BaseModel):
    method: str
    path: str
    #: What it does, for somebody building the screen rather than calling it.
    what: str
    #: True for anything that changes data, so a frontend can style it.
    writes: bool


class SectionOut(BaseModel):
    key: str
    name: str
    audience: Literal["super_admin", "admin", "everyone"]
    description: str
    #: The risk worth naming before somebody opens it, where there is one.
    caution: str | None
    endpoints: list[EndpointOut]
    #: Live figures for this section. Free-shaped because what is worth showing
    #: about a mailbox is not what is worth showing about a ranking — but every
    #: one carries ``needs_attention``, so a tile can badge itself without
    #: knowing which section it is drawing.
    status: dict[str, Any]


class ConsoleOut(BaseModel):
    sections: list[SectionOut]
    #: The sum across sections: failed messages, failed deliveries, a mirror
    #: that has never synced. Zero means nothing here wants looking at.
    needs_attention: int
    generated_at: datetime


class RoleHolderOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    email: str | None


class PermissionRuleOut(BaseModel):
    """One rule, in words a screen can print."""

    area: str
    what: str
    #: Role keys. Empty where the answer is not a role at all — "its author",
    #: "anyone on a team with the module" — in which case ``note`` says so.
    who: list[str]
    note: str | None
    #: Who currently holds those global roles, so an admin screen can answer
    #: "who can actually do this today" rather than only "which role can".
    holders: list[RoleHolderOut]
