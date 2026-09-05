"""Who may see and do what in HR.

HR data is the most sensitive thing in this system. An offer letter carries
somebody's salary; a performance review carries somebody's colleagues' opinion
of them. So the rules here are written out explicitly rather than left to a
general admin check, and they are narrow by default.

**Who HR is** is not decided here. It is membership of the HR team, which the
leave module already defines and which an organisation sets in one place —
``leave settings → hr_team_slug``. Two definitions of "HR" that can disagree is
one definition too many, so this delegates rather than re-deriving it.

**Global admin is not HR.** ``manager`` in particular is an organisation-wide
role over teams and work assignment, and giving it the personnel files as a
side effect is exactly the kind of quiet privilege creep that makes people stop
trusting the system. Super admin and the CEO are admitted, because both can
already put themselves on the HR team and pretending otherwise only makes the
audit trail worse.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.leave import service as leave_service
from app.models.hr import CycleStatus, EmployeeDocument, PerformanceReview, ReviewStatus
from app.models.user import User
from app.roles.deps import CurrentRoles

#: Global roles that reach HR without being on the HR team.
HR_ADMINS: Final[frozenset[str]] = frozenset({"super_admin", "ceo"})

#: Who may **destroy** HR data. Deliberately narrower than everything else in
#: this module — narrower even than HR itself.
#:
#: Reading a personnel file is recoverable from; deleting one is not. An offer
#: letter, a signed contract, the reviews behind somebody's promotion: these are
#: the records an organisation is asked to produce years later, sometimes by a
#: tribunal, and the person who most wants them gone is usually the person the
#: complaint is about. So the ability to remove them is held by one role and not
#: delegated to the team that works with them daily.
#:
#: Not ``HR_ADMINS``: the CEO is admitted to *read* HR because they can put
#: themselves on the HR team anyway and pretending otherwise only damages the
#: audit trail. Destroying evidence is a different question, and the answer to
#: it is a single role.
PURGE_ADMINS: Final[frozenset[str]] = frozenset({"super_admin"})


async def is_hr(session: AsyncSession, user_id: uuid.UUID, roles: set[str]) -> bool:
    if roles & HR_ADMINS:
        return True
    return await leave_service.is_hr(session, user_id)


async def require_hr(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    if not await is_hr(session, user.id, roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "HR only. Membership of the HR team is what grants this — the "
                "team named in leave settings — not a global admin role."
            ),
        )
    return user


#: The caller is on the HR team (or is a super admin / the CEO).
HRUser = Annotated[User, Depends(require_hr)]


async def require_purge_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    """Only a super admin, and deliberately not by way of the HR team.

    Note this does not consult team membership at all. Every other check in
    this module asks "are they HR"; this one asks "are they *the* administrator",
    which is a question team membership cannot answer.
    """
    if not roles & PURGE_ADMINS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only a super admin can delete HR records. Being on the HR team "
                "is not enough — deleting a personnel record is not recoverable, "
                "so it is held by one role."
            ),
        )
    return user


#: The caller is a super admin. Required by everything that destroys a record.
PurgeAdmin = Annotated[User, Depends(require_purge_admin)]


async def hr_flag(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> bool:
    """Whether the caller is HR, for endpoints open to everyone that show more to HR."""
    return await is_hr(session, user.id, roles)


IsHR = Annotated[bool, Depends(hr_flag)]


# ── record-level rules ─────────────────────────────────────────────────


def may_read_document(document: EmployeeDocument, *, viewer_id: uuid.UUID, hr: bool) -> bool:
    """HR always; the person it is about only when HR has shared it.

    Everybody else, including that person's manager, gets nothing. A manager
    who needs somebody's contract can ask HR for it, and that request being
    visible is a feature.
    """
    if hr:
        return True
    return document.user_id == viewer_id and document.visible_to_employee


def may_read_review(review: PerformanceReview, *, viewer_id: uuid.UUID, hr: bool) -> bool:
    """HR, the reviewer who wrote it, and the subject once it is shared.

    "Shared" is two conditions, not one: the cycle has to be marked as shared
    with subjects *and* the review has to actually be submitted. Letting a
    subject watch a half-finished review of themselves appear line by line
    would change what reviewers are willing to write.
    """
    if hr or review.reviewer_id == viewer_id:
        return True
    if review.subject_id != viewer_id:
        return False
    return (
        review.status == ReviewStatus.SUBMITTED
        and review.cycle is not None
        and review.cycle.shared_with_subjects
    )


def may_write_review(review: PerformanceReview, *, viewer_id: uuid.UUID) -> tuple[bool, str]:
    """Only the nominated reviewer, and only while the cycle is open.

    HR is deliberately excluded from *writing* somebody else's review even
    though it can read every one. An assessment attributed to a person who did
    not write it is not evidence about anybody.
    """
    if review.reviewer_id != viewer_id:
        return False, "Only the nominated reviewer can fill this in."
    if review.cycle is not None and review.cycle.status != CycleStatus.OPEN:
        return False, (
            "This review cycle is not open."
            if review.cycle.status == CycleStatus.DRAFT
            else "This review cycle has closed."
        )
    if review.status == ReviewStatus.SUBMITTED:
        return False, "Already submitted. Ask HR to reopen it if it needs changing."
    return True, ""
