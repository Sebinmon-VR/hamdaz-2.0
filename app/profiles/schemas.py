"""Response shapes for the user-profile endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SectionInfo(BaseModel):
    key: str
    label: str
    #: Calls out to Entra; skipped when ``local_only`` is set.
    remote: bool
    #: Left out of the default response unless asked for by name.
    heavy: bool
    #: Whether a reset removes this section's data.
    resettable: bool


class ProfileMeta(BaseModel):
    requested: list[str]
    #: Wall clock for the whole fan-out — the slowest section, not the sum.
    elapsed_ms: int
    section_ms: dict[str, int]


class UserProfileOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    #: One entry per section, keyed as in /users/sections.
    sections: dict[str, Any]
    #: Sections that failed, and why. A broken section does not fail the profile.
    errors: dict[str, str]
    meta: ProfileMeta


class ResetResult(BaseModel):
    user_id: str
    email: str
    display_name: str
    #: False for a reset, True when the account row itself was removed.
    account_deleted: bool
    #: Rows removed, per section.
    removed: dict[str, int]
    #: What they had immediately before, so the change is auditable.
    previous: dict[str, Any]
