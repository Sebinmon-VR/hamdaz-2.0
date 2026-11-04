"""Who may see payroll, who may change it, and who signs it off.

Four questions, four different answers, written out here rather than left to
a general admin check — the same reasoning as HR: a payslip is somebody's
salary, and "a manager can see it" is exactly the privilege creep nobody asked
for.

* **Seeing it** — the Accounts team (the team named in payroll settings), the
  super admins, the CEO, and anybody a super admin has put on the payroll
  access list. ``manager`` is *not* on that list: it is an organisation-wide
  role over teams and work, and the salaries do not come with it.
* **Processing it** — the people a super admin has marked *processor* on the
  access list, and the super admins themselves (``PROCESS_ROLES``). Not the
  Accounts team as a whole: reading is not editing.
* **Approving a month** — a super admin or the CEO. The CEO never approves a
  month they submitted: preparing and signing off your own payroll is one
  control, not two.
* **Super admins may do anything** (the user, 2026-11-04) — read, process,
  approve even their own submission, reopen an approved or paid month, and
  keep the access list. ``ADMIN_ROLES`` is that set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.models.payroll import AccessLevel, PayrollMember, PayrollSettings
from app.models.role import Role, UserRole
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.roles.deps import CurrentRoles

#: Global roles that read payroll without being on any list.
VIEW_ROLES: Final[frozenset[str]] = frozenset({"super_admin", "ceo"})
#: Global roles that process payroll without being on the list.
#:
#: Super admins, at the user's request (2026-11-04: "let the super admins do
#: anything"). Empty this set to go back to "processors are only the people on
#: the list".
PROCESS_ROLES: Final[frozenset[str]] = frozenset({"super_admin"})
#: Who approves a submitted month.
APPROVE_ROLES: Final[frozenset[str]] = frozenset({"super_admin", "ceo"})
#: Who may do anything: keep the access list, approve their own submission,
#: reopen an approved or paid month.
ADMIN_ROLES: Final[frozenset[str]] = frozenset({"super_admin"})


@dataclass(frozen=True, slots=True)
class PayrollAccess:
    user_id: uuid.UUID
    can_view: bool
    can_process: bool
    can_approve: bool
    can_admin: bool
    #: Why they can see it, in words — shown on their own access screen.
    via: tuple[str, ...]


async def accounts_slug(session: AsyncSession) -> str:
    slug = await session.scalar(select(PayrollSettings.accounts_team_slug).where(
        PayrollSettings.id == 1
    ))
    return slug or "accounts"


async def accounts_member_ids(session: AsyncSession) -> set[uuid.UUID]:
    slug = await accounts_slug(session)
    rows = await session.scalars(
        select(TeamMembership.user_id)
        .join(Team, Team.id == TeamMembership.team_id)
        .where(Team.slug == slug, Team.archived_at.is_(None))
    )
    return set(rows.all())


async def resolve(session: AsyncSession, user_id: uuid.UUID, roles: set[str]) -> PayrollAccess:
    via: list[str] = []
    if "super_admin" in roles:
        via.append("super admin")
    if "ceo" in roles:
        via.append("CEO")
    if user_id in await accounts_member_ids(session):
        via.append("Accounts team")
    member = await session.scalar(select(PayrollMember).where(PayrollMember.user_id == user_id))
    if member is not None:
        via.append(
            "payroll processor" if member.level == AccessLevel.PROCESSOR else "payroll access list"
        )
    return PayrollAccess(
        user_id=user_id,
        can_view=bool(via),
        can_process=bool(roles & PROCESS_ROLES)
        or (member is not None and member.level == AccessLevel.PROCESSOR),
        can_approve=bool(roles & APPROVE_ROLES),
        can_admin=bool(roles & ADMIN_ROLES),
        via=tuple(via),
    )


Session = Annotated[AsyncSession, Depends(get_session)]


async def current_access(user: CurrentUser, roles: CurrentRoles, session: Session) -> PayrollAccess:
    return await resolve(session, user.id, roles)


#: Everyone signed in gets an answer; nothing is refused. For ``/payroll/me``.
Access = Annotated[PayrollAccess, Depends(current_access)]


async def require_viewer(access: Access) -> PayrollAccess:
    if not access.can_view:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Payroll is limited to the Accounts team, the super admins, the "
                "CEO and people a super admin has added to the payroll access list."
            ),
        )
    return access


async def require_processor(access: Access) -> PayrollAccess:
    if not access.can_process:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only a payroll processor or a super admin can change payroll. "
                "A super admin names the processors on the payroll access list."
            ),
        )
    return access


async def require_approver(access: Access) -> PayrollAccess:
    if not access.can_approve:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin or the CEO can approve or return a payroll month.",
        )
    return access


async def require_admin(access: Access) -> PayrollAccess:
    if not access.can_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can do that.",
        )
    return access


Viewer = Annotated[PayrollAccess, Depends(require_viewer)]
Processor = Annotated[PayrollAccess, Depends(require_processor)]
Approver = Annotated[PayrollAccess, Depends(require_approver)]
Admin = Annotated[PayrollAccess, Depends(require_admin)]


async def approver_users(session: AsyncSession) -> list[User]:
    """Everyone who may approve — for the in-app notice on submission."""
    rows = await session.scalars(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.key.in_(APPROVE_ROLES), User.is_active.is_(True))
    )
    return list(dict.fromkeys(rows.all()))
