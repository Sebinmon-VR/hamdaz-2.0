"""Whether the person who had a tender can take it back today.

A stage change, a match found, a negotiation or an order comes back to
whoever had the tender before. That is right while they are here, and wrong
the moment they are not: a task handed to somebody serving notice, switched
off, or on leave sits with nobody. So before anything is handed back, the
person is checked here, and the reason they can't take it is said in words
the card and the task can carry ("Serving notice, last working day 30 Nov
2026").

Read live, like the labels: from the HR exit record, the account, and
approved leave on the office's day.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.hr.staff import office_today
from app.labels.service import leaving_reasons, on_leave_today
from app.models.user import User


async def away_reason(
    session: AsyncSession, email: str | None, *, on: date | None = None
) -> str | None:
    """Why ``email`` can't take work today, or ``None`` when they can.

    Somebody this app has no account for is not refused: a name on an old
    Proposals row may be spelled differently here, and handing their tender
    to the rotation on a spelling would be worse than the gap it closes.
    """
    address = (email or "").strip().lower()
    if not address:
        return None
    user = await session.scalar(select(User).where(func.lower(User.email) == address))
    if user is None:
        return None
    day = on or office_today()
    leaving = await leaving_reasons(session, [user.id], on=day)
    if user.id in leaving:
        return leaving[user.id]
    if not user.is_active:
        return "Account switched off"
    away = await on_leave_today(session, [user.id], on=day)
    if user.id in away:
        return away[user.id]
    return None
