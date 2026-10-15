"""Keeping the local copy of the Proposals list current.

Read the list and upsert it into ``proposal_index``, so the live workload, the
ranking, follow-ups, Ariba and the quote pages can answer from Postgres instead
of asking SharePoint every time. Nothing here writes to SharePoint: the list is
read, and only read.

The mail intake's search over this copy — full-text, then OpenAI embeddings,
then a model — was removed on 2026-10-14 with the intake's mail sorting.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import bindparam, cast, func, literal_column, select
from sqlalchemy.dialects.postgresql import REGCONFIG, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.proposal_index import MirrorState, ProposalIndexItem
from app.proposals.sharepoint import ProposalTask, SharePointProposals

logger = logging.getLogger("hamdaz.proposals.mirror")

#: Every field the mirror holds, so a sync brings back what search needs rather
#: than the three columns the workload aggregate settles for.
MIRROR_FIELDS = (
    "Title,Status,Priority,AssignedToLookupId,AssignedTo,StartDate,DueDate,BCD,"
    "EndUser,SubmissionStatus,CurrentType,OrderStatus,Negotiation,zohpquoteno,"
    # Attachments is a boolean and nothing more: Graph says whether an item has
    # files and will not say what they are called. See ProposalTask.
    "Remarks,WorkingNotes,Created,Modified,Attachments"
)

def normalise(value: str | None) -> str:
    """Reference numbers with the punctuation taken out, for comparing.

    ``QT-1001`` and ``QT/1001`` and ``qt 1001`` are one number written three
    ways, and the list contains all three habits.
    """
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def _as_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _as_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def search_text_of(task: ProposalTask) -> str:
    """The words a row is matched on.

    Title first and repeated nowhere else: it carries most of the meaning, and
    the tsvector below weights it accordingly. The notes are included because
    the customer's own reference is often only ever written there.
    """
    parts = [
        task.title,
        task.end_user,
        task.quote_no,
        task.current_type,
        task.submission_status,
        task.remarks,
        task.working_notes,
    ]
    return "\n".join(p.strip() for p in parts if p and str(p).strip())


def _hash(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class SyncReport:
    #: The newest Modified among the rows read, so the loop that watches the
    #: list knows what "unchanged since the sync" means.
    newest_modified: str | None = None
    read: int = 0
    changed: int = 0
    embedded: int = 0
    removed: int = 0
    duration_ms: int = 0
    full: bool = True
    error: str | None = None


# ── syncing ────────────────────────────────────────────────────────────


async def state(session: AsyncSession) -> MirrorState:
    row = await session.get(MirrorState, 1)
    if row is None:
        row = MirrorState(id=1)
        session.add(row)
        await session.flush()
    return row


def _row_values(
    task: ProposalTask, text_value: str, people: dict[str, dict[str, str]]
) -> dict[str, Any]:
    person = people.get(str(task.assigned_to_lookup_id or "")) or {}
    return {
        "assigned_email": (person.get("email") or "").strip().lower() or None,
        "item_id": str(task.id),
        "title": task.title or "(untitled)",
        "status": task.status,
        "effective_status": task.effective_status or None,
        "priority": task.priority,
        "end_user": task.end_user,
        "quote_no": task.quote_no,
        "submission_status": task.submission_status,
        "current_type": task.current_type,
        "order_status": task.order_status,
        "negotiation": task.negotiation,
        "remarks": task.remarks,
        "working_notes": task.working_notes,
        "start_date": _as_date(task.start_date),
        "due_date": _as_date(task.due_date),
        "bid_closing_date": _as_date(task.bid_closing_date),
        "deadline": task.closing_date,
        "assigned_lookup_id": task.assigned_to_lookup_id,
        "assigned_name": task.assigned_to_name,
        "is_open": task.is_open,
        "is_active": task.is_active,
        "has_attachments": task.has_attachments,
        "sp_created_at": _as_datetime(task.created_at),
        "sp_modified_at": _as_datetime(task.modified_at),
        "search_text": text_value,
        "deleted": False,
        "last_seen_at": datetime.now(UTC),
        # Feeds the D-weighted part of the vector only; not a column.
        "notes_text": f"{task.remarks or ''} {task.working_notes or ''}",
    }


def _iso(value: date | datetime | None) -> str | None:
    """A stored date back as the string the task shape carries."""
    return value.isoformat() if value is not None else None


def to_task(row: ProposalIndexItem) -> ProposalTask:
    """A mirror row as the shape every reader already speaks.

    The inverse of :func:`_row_values`, and deliberately the only way out of
    this table into a task: two mappings would drift the first time a column is
    added, and they would drift silently.

    **is_open and is_active are not read from their columns.** They are stored
    for indexed filtering, but they are assertions about *today* — a bid closes
    overnight and a row nobody edited is wrong by morning. Every sync is a full
    read, so in practice the columns keep up; deriving them here anyway costs a
    date comparison and means a screen served from this table and one served
    from the list cannot disagree about a bid closing today, which they have
    done before. See ``ProposalTask.is_active``.
    """
    return ProposalTask(
        id=row.item_id,
        title=row.title,
        status=row.status,
        priority=row.priority,
        assigned_to_lookup_id=row.assigned_lookup_id,
        assigned_to_name=row.assigned_name,
        start_date=_iso(row.start_date),
        due_date=_iso(row.due_date),
        bid_closing_date=_iso(row.bid_closing_date),
        end_user=row.end_user,
        submission_status=row.submission_status,
        current_type=row.current_type,
        order_status=row.order_status,
        negotiation=row.negotiation,
        quote_no=row.quote_no,
        remarks=row.remarks,
        working_notes=row.working_notes,
        created_at=_iso(row.sp_created_at),
        modified_at=_iso(row.sp_modified_at),
        has_attachments=row.has_attachments,
    )


async def tasks_for(session: AsyncSession, lookup_id: str) -> list[ProposalTask]:
    """Every row assigned to this person, from the local copy.

    Keyed on the SharePoint lookup id — the column SharePoint's own ``$filter``
    uses — and not on the email. The email is denormalised onto the row for
    scoring and is null wherever the site's people list no longer resolves the
    assignee; matching on it would answer a slightly different question than
    the list would.

    **No limit, which is the entire reason to read this table.** The list client
    pages and then truncates to its first few hundred rows, in SharePoint's own
    order — item id, so oldest first — before any caller can sort. A live bid is
    a recent row by definition, so that ceiling removed precisely the rows a
    person is looking for. Here the filter is an indexed column and the whole
    answer comes back.
    """
    rows = (
        await session.scalars(
            select(ProposalIndexItem).where(
                ProposalIndexItem.deleted.is_(False),
                ProposalIndexItem.assigned_lookup_id == str(lookup_id),
            )
        )
    ).all()
    return [to_task(row) for row in rows]


def _vector_expression():
    """The tsvector, weighted by where the words came from.

    A hit in the title is worth more than one in somebody's working notes, and
    Postgres' own ranking understands that if the weights are set. Built in the
    application rather than as a generated column so the weighting is visible
    here and changeable without a migration.

    Written against *bound parameters* rather than a row's values, so the one
    statement serves every row and the whole list goes to Postgres in a few
    batches. Built per row it was thirteen hundred round trips to Azure, which
    took longer than the interval the loop runs on.
    """
    # Both constants are typed explicitly. SQLAlchemy's psycopg dialect renders
    # a plain string bind as ``'english'::VARCHAR``, and Postgres has no
    # ``to_tsvector(varchar, ...)`` nor ``setweight(tsvector, varchar)`` — the
    # sync failed on the first row with "function does not exist" until it did.
    def w(param: str, weight: str):
        assert weight in ("A", "B", "C", "D")
        return func.setweight(
            func.to_tsvector(
                cast("english", REGCONFIG), func.coalesce(bindparam(param), "")
            ),
            literal_column(f"'{weight}'"),
        )

    return (
        w("title", "A")
        .op("||")(w("quote_no", "A"))
        .op("||")(w("end_user", "B"))
        .op("||")(w("current_type", "C"))
        .op("||")(w("notes_text", "D"))
    )


async def sync(
    session: AsyncSession,
    sharepoint: SharePointProposals,
) -> SyncReport:
    """Bring the mirror in line with the list.

    A full read every time, and at this size that is the right trade: the list
    is around thirteen hundred rows and comes back in about two seconds, which
    is nothing in a background loop and is always correct. What is *not* done
    every time is the expensive part — a row whose words have not changed keeps
    its embedding, so the steady-state cost of staying current is one SharePoint
    read and no model calls at all.

    Rows that have left the list are marked gone rather than deleted, so an
    intake record or a report that referred to one still resolves to something.
    """
    started = time.monotonic()
    report = SyncReport()
    record = await state(session)

    try:
        # Together rather than back to back: they are independent, and the
        # people list is what lets a row carry the assignee's email so that
        # scoring never has to ask SharePoint anything.
        tasks, people = await asyncio.gather(
            sharepoint.all_tasks(fields=MIRROR_FIELDS), sharepoint.site_people()
        )
    except Exception as exc:  # noqa: BLE001 - a stale mirror beats a broken one
        record.last_error = f"{type(exc).__name__}: {exc}"
        await session.flush()
        report.error = record.last_error
        return report

    report.read = len(tasks)
    report.newest_modified = max(
        (t.modified_at for t in tasks if t.modified_at), default=None
    )
    # Only the two columns the comparison needs: a full row here carries the
    # embedding, and thirteen hundred of those is most of a megabyte for nothing.
    hashes: dict[str, str | None] = dict(
        (
            await session.execute(
                select(ProposalIndexItem.item_id, ProposalIndexItem.text_hash).where(
                    ProposalIndexItem.item_id.in_([str(t.id) for t in tasks])
                )
            )
        ).all()
    ) if tasks else {}

    rows: list[dict[str, Any]] = []
    for task in tasks:
        text_value = search_text_of(task)
        digest = _hash(text_value)
        values = _row_values(task, text_value, people)
        values["text_hash"] = digest
        rows.append(values)

        if str(task.id) not in hashes or hashes[str(task.id)] != digest:
            report.changed += 1

    if rows:
        # One statement, many parameter sets. Every column is a bound
        # parameter and the vector is an expression over those same
        # parameters, so the driver ships the list in batches rather than a
        # round trip per row.
        columns = [k for k in rows[0] if k != "notes_text"]
        statement = insert(ProposalIndexItem).values(
            {**{c: bindparam(c) for c in columns}, "search_vector": _vector_expression()}
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ProposalIndexItem.item_id],
            set_={
                c: getattr(statement.excluded, c)
                for c in [*columns, "search_vector"]
                if c != "item_id"
            },
        )
        for start in range(0, len(rows), _UPSERT_BATCH):
            await session.execute(statement, rows[start : start + _UPSERT_BATCH])

    # Anything we hold that the list no longer returns has gone from it.
    seen = [str(t.id) for t in tasks]
    if seen:
        gone = (
            await session.scalars(
                select(ProposalIndexItem).where(
                    ProposalIndexItem.item_id.notin_(seen),
                    ProposalIndexItem.deleted.is_(False),
                )
            )
        ).all()
        for row in gone:
            row.deleted = True
        report.removed = len(gone)

    await session.flush()

    report.duration_ms = int((time.monotonic() - started) * 1000)
    record.last_sync_at = datetime.now(UTC)
    record.last_full_sync_at = record.last_sync_at
    record.rows_read = report.read
    record.rows_changed = report.changed
    record.rows_embedded = report.embedded
    record.duration_ms = report.duration_ms
    record.last_error = None
    await session.flush()
    return report


#: Rows per upsert statement. Large enough that a full list is a handful of
#: calls; small enough that one refused row is findable.
_UPSERT_BATCH = 200

# ── the numbers the mirror can answer without SharePoint ───────────────


def _iso(value: date | None) -> str | None:
    """A stored date, in the shape SharePoint sends one.

    The list gives ``2026-09-11T00:00:00Z`` and everything downstream parses
    that into an aware datetime. A bare ``2026-09-11`` parses too — into a
    naive one, which then cannot be compared with "now" and the whole
    recompute fell over on the first deadline.
    """
    return f"{value.isoformat()}T00:00:00Z" if value is not None else None


async def as_tasks(
    session: AsyncSession,
) -> tuple[list[ProposalTask], dict[str, dict[str, str]]]:
    """The mirror, in the shape the existing analytics already understand.

    Rebuilding ``ProposalTask`` objects rather than counting the rows here is
    deliberate. ``app.proposals.analytics.summarise`` is pure, tested, and
    already knows what "overdue" means once a bid has closed and what to do
    with the fifth of the list that has no status — and the whole point of the
    live scoring is that it produces the *same* answer as the on-demand run,
    from a cheaper source. Recounting it independently would be two
    definitions of somebody's workload, and they would disagree eventually.
    """
    # Only the columns a count needs. The whole row carries the embedding and
    # the search text — most of a megabyte across the list, and no part of
    # anybody's workload — and against a database a hundred milliseconds away
    # that was the slowest step of every recompute.
    c = ProposalIndexItem
    rows = (
        await session.execute(
            select(
                c.item_id, c.title, c.status, c.priority, c.assigned_lookup_id,
                c.assigned_name, c.assigned_email, c.start_date, c.due_date,
                c.bid_closing_date, c.end_user, c.submission_status, c.current_type,
                c.order_status, c.negotiation, c.quote_no, c.remarks, c.working_notes,
                c.sp_created_at, c.sp_modified_at,
            ).where(c.deleted.is_(False))
        )
    ).all()

    tasks = [
        ProposalTask(
            id=row.item_id,
            title=row.title,
            status=row.status,
            priority=row.priority,
            assigned_to_lookup_id=row.assigned_lookup_id,
            assigned_to_name=row.assigned_name,
            start_date=_iso(row.start_date),
            due_date=_iso(row.due_date),
            bid_closing_date=_iso(row.bid_closing_date),
            end_user=row.end_user,
            submission_status=row.submission_status,
            current_type=row.current_type,
            order_status=row.order_status,
            negotiation=row.negotiation,
            quote_no=row.quote_no,
            remarks=row.remarks,
            working_notes=row.working_notes,
            created_at=row.sp_created_at.isoformat() if row.sp_created_at else None,
            modified_at=row.sp_modified_at.isoformat() if row.sp_modified_at else None,
        )
        for row in rows
    ]

    people: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.assigned_lookup_id:
            people.setdefault(
                str(row.assigned_lookup_id),
                {"name": row.assigned_name or "", "email": row.assigned_email or ""},
            )
    return tasks, people


async def last_assigned(session: AsyncSession) -> dict[str, datetime]:
    """When each person last picked something up, by lookup id.

    Read from the mirror's own created dates rather than by asking SharePoint
    for the newest row per person, which is what the on-demand path does.
    """
    out: dict[str, datetime] = {}
    rows = (
        await session.execute(
            select(
                ProposalIndexItem.assigned_lookup_id,
                func.max(ProposalIndexItem.sp_created_at),
            )
            .where(
                ProposalIndexItem.deleted.is_(False),
                ProposalIndexItem.assigned_lookup_id.is_not(None),
            )
            .group_by(ProposalIndexItem.assigned_lookup_id)
        )
    ).all()
    for lookup_id, newest in rows:
        if newest is not None:
            out[str(lookup_id)] = newest
    return out


@dataclass(slots=True)
class Workload:
    """One person's task counts, straight out of the mirror."""

    lookup_id: str
    name: str | None = None
    email: str | None = None
    total: int = 0
    open_tasks: int = 0
    active: int = 0
    completed: int = 0
    overdue: int = 0
    due_soon: int = 0
    no_status: int = 0
    last_assigned_at: datetime | None = None
    titles: list[str] = field(default_factory=list)


async def workload(
    session: AsyncSession, *, due_soon_days: int = 7
) -> dict[str, Workload]:
    """Counts per person, keyed by SharePoint lookup id.

    This is the reason the live scoring can run on every change. Counting the
    mirror is one grouped query against an indexed table — a millisecond or two
    — where the same answer from SharePoint means pulling every row again.
    """
    today = date.today()
    rows = (
        await session.scalars(
            select(ProposalIndexItem).where(
                ProposalIndexItem.deleted.is_(False),
                ProposalIndexItem.assigned_lookup_id.is_not(None),
            )
        )
    ).all()

    out: dict[str, Workload] = {}
    for row in rows:
        key = str(row.assigned_lookup_id)
        entry = out.setdefault(
            key,
            Workload(lookup_id=key, name=row.assigned_name, email=row.assigned_email),
        )
        entry.name = entry.name or row.assigned_name
        entry.email = entry.email or row.assigned_email
        entry.total += 1
        if row.is_open:
            entry.open_tasks += 1
        else:
            entry.completed += 1
        if row.is_active:
            entry.active += 1
        if not (row.status or "").strip():
            entry.no_status += 1
        if row.is_open and row.deadline is not None:
            days = (row.deadline - today).days
            if days < 0:
                entry.overdue += 1
            elif days <= due_soon_days:
                entry.due_soon += 1
        if row.sp_created_at is not None and (
            entry.last_assigned_at is None or row.sp_created_at > entry.last_assigned_at
        ):
            entry.last_assigned_at = row.sp_created_at
    return out


class MirrorSync:
    """Serialises syncs so two callers cannot run one at the same time.

    Held on the application rather than created per request. The lock matters
    because a sync is an upsert over the whole list: two at once would not
    corrupt anything, but they would double the SharePoint reads and the
    embedding bill for no benefit.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.last: SyncReport | None = None

    async def run(
        self,
        session: AsyncSession,
        sharepoint: SharePointProposals,
    ) -> SyncReport:
        async with self._lock:
            self.last = await sync(session, sharepoint)
            return self.last
