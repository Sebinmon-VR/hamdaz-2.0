"""The priority score, kept current instead of computed when asked.

The runs in ``service.py`` answer "why did Rahul get that proposal in March".
This answers "who should get the next one" — a different question with exactly
one right answer at any moment, so it is a row per person that is overwritten
rather than another run. Recomputing every few minutes as runs would bury the
handful that recorded a real decision under hundreds that recorded nothing.

**Why this can run on every change.** The counts come from the Proposals mirror
rather than from SharePoint. Counting the mirror is one query over an indexed
table; the same answer from SharePoint means pulling every row again, which is
why the score was previously computed only on demand and was therefore always
a little behind the work.

**None of the rules live here.** Capacity by label, the on-leave exclusion, the
manager exclusion, the open-work ceiling and the scoring itself are all
``service.gather`` and ``scoring.score``, called with counts from a different
source. Two rankings that disagreed would be worse than one that is a minute
stale, so there is one copy of the rules and this is not it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.scoring import Scored, score
from app.analytics.service import gather, weights_of
from app.models.analytics import LiveScore
from app.models.team import Team
from app.proposals.analytics import summarise
from app.proposals.mirror import as_tasks, last_assigned

logger = logging.getLogger("hamdaz.analytics.live")


async def recompute(
    session: AsyncSession,
    *,
    team: Team | None = None,
    reason: str = "mirror",
) -> list[LiveScore]:
    """Rewrite the live standing for one team, or for everybody.

    The whole set is replaced rather than patched. Somebody who left the team,
    or who no longer holds any work, has to *stop* having a rank — and a patch
    that only touched the people whose counts moved would leave them ranked for
    ever.
    """
    tasks, people = await as_tasks(session)
    workload = summarise(tasks, people)
    seen = await last_assigned(session)

    candidates, policy, _ = await gather(
        session,
        sharepoint=None,
        cache=None,
        team=team,
        workload=workload,
        last_seen=seen,
    )
    ranked = score(candidates, weights=weights_of(policy)) if candidates else []
    rows = await _replace(session, team, ranked, reason=reason)
    logger.debug(
        "live scores: team=%s people=%d reason=%s",
        team.slug if team else "all", len(rows), reason,
    )
    return rows


async def _replace(
    session: AsyncSession,
    team: Team | None,
    ranked: Iterable[Scored],
    *,
    reason: str,
) -> list[LiveScore]:
    scope = (
        LiveScore.team_id == team.id if team is not None else LiveScore.team_id.is_(None)
    )
    await session.execute(delete(LiveScore).where(scope))

    now = datetime.now(UTC)
    rows: list[LiveScore] = []
    for result in ranked:
        person = result.candidate
        if person.user_id is None:
            # Holds proposal work but has never signed in here. They count
            # towards everybody else's load, which is why they were scored, but
            # there is no user row to hang a rank on.
            continue
        row = LiveScore(
            user_id=uuid.UUID(person.user_id),
            team_id=team.id if team is not None else None,
            display_name=person.display_name,
            email=person.email,
            sharepoint_lookup_id=person.lookup_id,
            total_tasks=person.total_tasks,
            open_tasks=person.open_tasks,
            active_tasks=person.active_tasks,
            completed_tasks=person.completed_tasks,
            overdue_tasks=person.overdue_tasks,
            due_soon_tasks=person.due_soon_tasks,
            no_status_tasks=person.no_status_tasks,
            days_since_assigned=(
                int(person.days_since_last_assign(now))
                if person.last_assigned_at is not None
                else None
            ),
            capacity=person.capacity,
            priority_score=result.factor_total or Decimal(0),
            # ``priority`` is the queue position and 1 is next. Somebody
            # excluded has no position at all; 0 keeps them from sorting ahead
            # of a person who does.
            rank=result.priority or 0,
            eligible=not result.excluded,
            excluded_reason=result.excluded_reason,
            factors=result.factors or {},
            reason=reason,
            computed_at=now,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def next_up(
    session: AsyncSession, *, team: Team | None = None, limit: int = 1
) -> list[LiveScore]:
    """Who should get the next task. Rank 1 first.

    An indexed lookup rather than a computation, which is the point of keeping
    the table: choosing who an incoming tender goes to has to be quick enough
    to happen while the email that brought it is still being handled.
    """
    scope = (
        LiveScore.team_id == team.id if team is not None else LiveScore.team_id.is_(None)
    )
    return list(
        (
            await session.scalars(
                select(LiveScore)
                .where(scope, LiveScore.eligible.is_(True), LiveScore.rank > 0)
                .order_by(LiveScore.rank)
                .limit(limit)
            )
        ).all()
    )


async def standing(
    session: AsyncSession, *, team: Team | None = None
) -> list[LiveScore]:
    """The whole current ranking, best first, with the excluded at the end."""
    scope = (
        LiveScore.team_id == team.id if team is not None else LiveScore.team_id.is_(None)
    )
    return list(
        (
            await session.scalars(
                select(LiveScore)
                .where(scope)
                .order_by(
                    LiveScore.eligible.desc(),
                    LiveScore.rank.asc(),
                    LiveScore.display_name.asc(),
                )
            )
        ).all()
    )
