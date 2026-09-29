"""When to visit Ariba, and what to keep from a visit.

**A visit is earned, not scheduled.** The tender email puts a new row in the
Proposals list; the mirror copies it here; and only a new row carrying a tender
number we do not already have is a reason to open the portal. Rows that arrive
together are waited out (``ariba_settle_seconds``) so a burst is one visit, and
two ceilings — a minimum gap and a daily cap — hold whatever else happens.

Nothing here writes to SharePoint. The events live in this database, matched
to their Proposals rows by tender number when they are read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ariba.portal import Visit
from app.core.config import Settings
from app.models.ariba import AribaEvent, AribaState
from app.models.proposal_index import ProposalIndexItem

OPEN = "Open"
CLOSED = "Closed"
#: The Proposals SubmissionStatus that says the bid went in, lower-cased.
SUBMITTED = "submitted"


def references_in(pattern: str, text: str | None) -> list[str]:
    return list(dict.fromkeys(re.findall(pattern, text or "")))


async def state(session: AsyncSession) -> AribaState:
    record = await session.get(AribaState, 1)
    if record is None:
        record = AribaState(id=1, visits_today=0)
        session.add(record)
        await session.flush()
    return record


@dataclass(slots=True)
class Pending:
    #: Tender numbers in new Proposals rows that no event carries yet.
    references: list[str]
    #: The newest ``sp_created_at`` looked at — the next watermark.
    newest: datetime | None
    #: Tender numbers in new rows that an event already carries: no visit is
    #: needed, but the new row's BCD still has to be checked against it.
    known: list[str] = field(default_factory=list)


async def pending(session: AsyncSession, settings: Settings, record: AribaState) -> Pending:
    """New Proposals rows since the watermark whose tender we have not read."""
    query = select(ProposalIndexItem.title, ProposalIndexItem.sp_created_at).where(
        ProposalIndexItem.deleted.is_(False),
        ProposalIndexItem.sp_created_at.is_not(None),
    )
    if record.watermark is not None:
        query = query.where(ProposalIndexItem.sp_created_at > record.watermark)
    rows = (await session.execute(query)).all()
    if not rows:
        return Pending([], None)
    newest = max(created for _, created in rows)
    if record.watermark is None:
        # First run: the history is not a reason to visit. Start from now,
        # with one visit to fill the table.
        return Pending(["*"], newest)

    found: list[str] = []
    for title, _ in rows:
        for ref in references_in(settings.ariba_reference_pattern, title):
            if ref not in found:
                found.append(ref)
    if found:
        known = set(
            (
                await session.scalars(
                    select(AribaEvent.reference).where(AribaEvent.reference.in_(found))
                )
            ).all()
        )
        return Pending(
            [r for r in found if r not in known], newest, [r for r in found if r in known]
        )
    return Pending(found, newest)


def refusal(
    settings: Settings, record: AribaState, newest: datetime, now: datetime, *, force: bool = False
) -> str | None:
    """Why a visit may not happen yet, or ``None`` when it may.

    ``force`` — somebody asking now — skips the settle and the gap, never the
    pause after a refused sign-in or the daily cap.
    """
    if record.paused_until and now < record.paused_until:
        return f"paused until {record.paused_until:%Y-%m-%d %H:%M} UTC after a refused sign-in"
    if record.visits_on == now.date() and record.visits_today >= settings.ariba_max_visits_per_day:
        return "the day's visits are used"
    if force:
        return None
    if now - newest < timedelta(seconds=settings.ariba_settle_seconds):
        return "waiting for the rest of the burst"
    if record.last_visit_at and now - record.last_visit_at < timedelta(
        seconds=settings.ariba_min_gap_seconds
    ):
        return "visited too recently"
    return None


def count_visit(record: AribaState, now: datetime) -> None:
    if record.visits_on != now.date():
        record.visits_on = now.date()
        record.visits_today = 0
    record.visits_today += 1
    record.last_visit_at = now


async def keep(
    session: AsyncSession, settings: Settings, record: AribaState, visit: Visit, now: datetime
) -> str:
    """Save what one visit read. Returns a one-line summary for the state row."""
    record.session_state = visit.session_state
    if visit.signed_in:
        record.last_login_at = now

    known = set((await session.scalars(select(AribaEvent.doc_id))).all())
    rows = [
        {
            "doc_id": e.doc_id,
            "reference": next(iter(references_in(settings.ariba_reference_pattern, e.title)), None),
            "title": e.title,
            "end_time": e.end_time,
            "status": OPEN,
            "participated": e.participated,
            "first_seen_at": now,
            "last_seen_at": now,
        }
        for e in visit.events
    ]
    if rows:
        statement = insert(AribaEvent).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=[AribaEvent.doc_id],
            set_={
                c: getattr(statement.excluded, c)
                for c in (
                    "reference", "title", "end_time", "status", "participated", "last_seen_at"
                )
            }
            | {"updated_at": now},
        )
        await session.execute(statement)

    seen = [e.doc_id for e in visit.events]
    closed = await session.execute(
        update(AribaEvent)
        .where(AribaEvent.status == OPEN, AribaEvent.doc_id.notin_(seen or [""]))
        .values(status=CLOSED, updated_at=now)
    )
    added = len([d for d in seen if d not in known])
    return f"{len(seen)} open, {added} new, {closed.rowcount} closed"


def _plain(text: str | None) -> str:
    """A title for comparing: lower case, words only — so " EOI - LTPA for VR"
    and "EOI – LTPA for VR" are one title."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


#: A title this short says too little to be matched on its own.
_MIN_TITLE = 20


def match[T](
    settings: Settings, events: Sequence[AribaEvent], rows: Sequence[T]
) -> dict[str, list[T]]:
    """Each event's Proposals rows, keyed by ``doc_id``. ``rows`` are anything
    with a ``title``.

    By tender number when the event has one — every live row carrying it. When
    it has none (an EOI often does not), by title: the same words, or one title
    containing the other. A title match counts only when exactly one row has
    it; guessing between two is not matching.
    """
    by_ref: dict[str, list[T]] = {}
    plain = [(_plain(getattr(r, "title", None)), r) for r in rows]
    for text, row in plain:
        for ref in references_in(settings.ariba_reference_pattern, text):
            by_ref.setdefault(ref, []).append(row)

    out: dict[str, list[T]] = {}
    for event in events:
        if event.reference:
            out[event.doc_id] = by_ref.get(event.reference, [])
            continue
        wanted = _plain(event.title)
        if len(wanted) < _MIN_TITLE:
            out[event.doc_id] = []
            continue
        same = [r for t, r in plain if t == wanted]
        if not same:
            same = [
                r for t, r in plain if len(t) >= _MIN_TITLE and (wanted in t or t in wanted)
            ]
        out[event.doc_id] = same if len(same) == 1 else []
    return out


async def _live_rows(session: AsyncSession) -> list:
    return (
        await session.execute(
            select(
                ProposalIndexItem.item_id,
                ProposalIndexItem.title,
                ProposalIndexItem.submission_status,
                ProposalIndexItem.sp_modified_at,
            ).where(ProposalIndexItem.deleted.is_(False))
        )
    ).all()


async def events(
    session: AsyncSession, settings: Settings, *, status: str | None = None
) -> list[dict]:
    """Events, soonest closing first, each with the Proposals row it matches."""
    query = select(AribaEvent).order_by(AribaEvent.end_time.asc().nulls_last())
    if status:
        query = query.where(AribaEvent.status == status)
    found = (await session.scalars(query)).all()
    matched = match(settings, found, await _live_rows(session)) if found else {}

    out = []
    for e in found:
        rows = matched.get(e.doc_id) or []
        row = rows[0] if rows else None
        submission = row.submission_status if row else None
        out.append(
            {
                "doc_id": e.doc_id,
                "reference": e.reference,
                "title": e.title,
                "end_time": e.end_time,
                "status": e.status,
                "participated": e.participated,
                "first_seen_at": e.first_seen_at,
                "last_seen_at": e.last_seen_at,
                "proposal_item_id": row.item_id if row else None,
                "proposal_title": row.title.strip() if row else None,
                "submission_status": submission,
                "not_received": _not_received(e, submission),
            }
        )
    return out


def _not_received(event: AribaEvent, submission: str | None) -> bool:
    """The task says Submitted, but Ariba — as of the last visit — has no
    response from us on a tender that is still open."""
    return (
        event.status == OPEN
        and event.participated is False
        and (submission or "").strip().lower() == SUBMITTED
    )


#: A visit's own BCD corrections land in the minutes after it starts and move
#: the row's Modified stamp; changes that soon after a visit are not a reason
#: for another one.
_OWN_WRITES = timedelta(minutes=10)


async def needs_recheck(
    session: AsyncSession, settings: Settings, record: AribaState
) -> datetime | None:
    """A reason to visit with nothing new arriving: a task was marked Submitted
    after the visit that saw its tender with no response. The warning would
    otherwise stand until some unrelated tender brought the reader back.

    Returns when the newest such task changed, or ``None`` when none did.
    """
    if record.last_visit_at is None:
        return None
    suspect = (
        await session.scalars(
            select(AribaEvent).where(
                AribaEvent.status == OPEN, AribaEvent.participated.is_(False)
            )
        )
    ).all()
    if not suspect:
        return None
    after = record.last_visit_at + _OWN_WRITES
    changed = [
        row.sp_modified_at
        for rows in match(settings, suspect, await _live_rows(session)).values()
        for row in rows
        if (row.submission_status or "").strip().lower() == SUBMITTED
        and row.sp_modified_at is not None
        and row.sp_modified_at > after
    ]
    return max(changed, default=None)


def now_utc() -> datetime:
    return datetime.now(UTC)
