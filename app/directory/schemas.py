"""Response shapes for the directory endpoints."""

from __future__ import annotations

from pydantic import BaseModel

from app.directory.graph import OrgUser


class OrgUserOut(BaseModel):
    #: Entra object id. This is what the team module will store to point at a
    #: person, so it is the field that matters most in this payload.
    object_id: str
    display_name: str
    email: str | None
    user_principal_name: str
    job_title: str | None
    department: str | None
    office_location: str | None
    mobile_phone: str | None
    account_enabled: bool
    is_guest: bool

    @classmethod
    def from_domain(cls, user: OrgUser) -> OrgUserOut:
        return cls(
            object_id=user.object_id,
            display_name=user.display_name,
            email=user.email,
            user_principal_name=user.user_principal_name,
            job_title=user.job_title,
            department=user.department,
            office_location=user.office_location,
            mobile_phone=user.mobile_phone,
            account_enabled=user.account_enabled,
            is_guest=user.is_guest,
        )


class OrgUserPage(BaseModel):
    #: Matches after filtering, before the window is applied.
    total: int
    #: How many are in this response.
    count: int
    offset: int
    limit: int
    users: list[OrgUserOut]
