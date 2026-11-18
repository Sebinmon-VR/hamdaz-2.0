"""What the agent did, kept for the settings page: the audit trail.

The user's shortlist (2026-11-18, #6): every change the agent makes in the ERP
for somebody (a costing, a status answer, a leave request) is recorded here
with whose chat it was for, when, and whether it worked, before any of the
new writes go live. The same log holds the messages it sends unasked, the 👎
on its replies (feedback), and each turn's model spend.

Recording never stops the agent: a failed write is logged and dropped, and
without the table (the migration not run yet) nothing is kept.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.itsupport import ITSupportActivity

logger = logging.getLogger("hamdaz.itsupport")

#: What a row is about.
CHANGE: Final = "change"
ANSWER: Final = "answer"
SENT: Final = "sent"
FEEDBACK: Final = "feedback"
TURN: Final = "turn"


async def record(factory: async_sessionmaker[AsyncSession] | None, *, email: str, kind: str,
                 summary: str, detail: dict[str, Any] | None = None, ok: bool = True,
                 cost_usd: float | None = None) -> None:
    """One row, in its own session. Never raises."""
    if factory is None:
        return
    try:
        async with factory() as session:
            session.add(ITSupportActivity(
                email=email.lower(), kind=kind, summary=summary[:2000], detail=detail or {},
                ok=ok, cost_usd=Decimal(str(round(cost_usd, 5))) if cost_usd is not None
                else None))
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - the log is a record, not a gate
        logger.warning("it support: activity not recorded (%s): %s", kind, exc)


async def recent(session: AsyncSession, *, kind: str | None = None, email: str | None = None,
                 limit: int = 200) -> list[ITSupportActivity]:
    query = select(ITSupportActivity).order_by(ITSupportActivity.at.desc()).limit(limit)
    if kind:
        query = query.where(ITSupportActivity.kind == kind)
    else:
        # Turns are the spend; the page shows them as a total, not a row each.
        query = query.where(ITSupportActivity.kind != TURN)
    if email:
        query = query.where(ITSupportActivity.email == email.lower())
    return list((await session.scalars(query)).all())


async def spend_by_day(session: AsyncSession, *, days: int = 14) -> list[dict[str, Any]]:
    """Model spend per UAE day, newest first."""
    since = datetime.now(UTC) - timedelta(days=days)
    day = func.date(func.timezone("Asia/Dubai", ITSupportActivity.at))
    rows = await session.execute(
        select(day, func.coalesce(func.sum(ITSupportActivity.cost_usd), 0), func.count())
        .where(ITSupportActivity.kind == TURN, ITSupportActivity.at >= since)
        .group_by(day).order_by(day.desc()))
    return [{"day": str(d), "usd": round(float(usd), 4), "turns": n} for d, usd, n in rows.all()]
