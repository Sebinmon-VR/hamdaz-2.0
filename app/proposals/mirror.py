"""Keeping the local copy of the Proposals list current, and searching it.

Two jobs, in one file because the second is only correct if the first has run:

* **sync** — read the list and upsert it into ``proposal_index``, re-embedding
  only the rows whose words changed;
* **search** — find the rows an incoming email might be about, without asking
  SharePoint anything and without showing a language model a thousand rows.

Nothing here writes to SharePoint. The list is read, and only read.

The search is deliberately three stages, narrowing before it gets expensive —
full-text, then vectors, then the model. The reasoning is in
``app.models.proposal_index``; what matters here is that each stage is bounded,
so the cost of matching one email does not grow with the list.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import bindparam, cast, func, literal_column, or_, select
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
    "Remarks,WorkingNotes,Created,Modified"
)

#: How many rows the first stage hands to the second. Wide enough that the
#: right row is in it even when the wording is unhelpful, small enough that the
#: cosine over it is a rounding error.
CANDIDATE_LIMIT = 250

#: What comes out, for a model to adjudicate. More than this and the model gets
#: worse rather than better — the whole reason for the funnel.
SHORTLIST = 10

#: Small on purpose. The full 1536 dimensions buy very little on text this
#: short and cost three times the memory and storage.
EMBED_DIMENSIONS = 512

#: A reference that looks like a tender or quote number: letters, digits,
#: dashes and slashes, with at least one digit and some length to it. Used to
#: pull identifiers out of an email and to match them exactly, which is the
#: half of matching that no embedding does well.
_REFERENCE = re.compile(r"\b(?=[A-Za-z0-9/\-]{4,40}\b)(?=[^\s]*\d)[A-Za-z0-9][A-Za-z0-9/\-]{3,39}\b")


def references_in(*texts: str | None) -> list[str]:
    """Every plausible reference number in some text, upper-cased and unique."""
    found: list[str] = []
    for chunk in texts:
        for hit in _REFERENCE.findall(chunk or ""):
            token = hit.upper().strip("-/")
            if token and token not in found:
                found.append(token)
    return found


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


class Embedder:
    """Turns text into vectors, or into nothing at all.

    An unconfigured key is not an error here. Without embeddings the funnel
    still works — full-text finds the candidates and the model adjudicates
    them — it is only the middle stage that is skipped. Refusing to sync
    because a model key is missing would make the mirror, and therefore the
    live workload counts, depend on something they have nothing to do with.
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(getattr(self._settings, "openai_api_key", ""))

    def _openai(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.AsyncOpenAI(
                api_key=self._settings.openai_api_key,
                base_url=self._settings.openai_base_url or None,
                timeout=self._settings.openai_timeout_seconds,
            )
        return self._client

    async def embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        """One vector per text, or None if embeddings are not available."""
        if not texts or not self.available:
            return None
        try:
            response = await self._openai().embeddings.create(
                model=getattr(self._settings, "embedding_model", "text-embedding-3-small"),
                input=list(texts),
                dimensions=EMBED_DIMENSIONS,
            )
        except Exception as exc:  # noqa: BLE001 - never fail a sync over this
            logger.warning("embedding failed, continuing without: %s", exc)
            return None
        return [row.embedding for row in response.data]


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
        "sp_created_at": _as_datetime(task.created_at),
        "sp_modified_at": _as_datetime(task.modified_at),
        "search_text": text_value,
        "deleted": False,
        "last_seen_at": datetime.now(UTC),
        # Feeds the D-weighted part of the vector only; not a column.
        "notes_text": f"{task.remarks or ''} {task.working_notes or ''}",
    }


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
    embedder: Embedder,
    *,
    embed: bool = True,
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

    needs_embedding: list[tuple[str, str]] = []
    rows: list[dict[str, Any]] = []
    for task in tasks:
        text_value = search_text_of(task)
        digest = _hash(text_value)
        rows.append(_row_values(task, text_value, people))

        if str(task.id) not in hashes or hashes[str(task.id)] != digest:
            report.changed += 1
            if embed:
                needs_embedding.append((str(task.id), text_value))

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

    if needs_embedding:
        report.embedded = await _embed_rows(session, embedder, needs_embedding)

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

#: Embedding requests are batched. One call per row would be a thousand round
#: trips on a first sync; the API takes many inputs at once and bills the same.
_EMBED_BATCH = 128


async def _embed_rows(
    session: AsyncSession, embedder: Embedder, rows: list[tuple[str, str]]
) -> int:
    done = 0
    for start in range(0, len(rows), _EMBED_BATCH):
        chunk = rows[start : start + _EMBED_BATCH]
        vectors = await embedder.embed([text_value for _, text_value in chunk])
        if vectors is None:
            # No key, or the call failed. The mirror is still correct and the
            # funnel still works without its middle stage.
            return done
        now = datetime.now(UTC)
        for (item_id, text_value), vector in zip(chunk, vectors, strict=False):
            item = await session.get(ProposalIndexItem, item_id)
            if item is None:
                continue
            item.embedding = list(vector)
            item.text_hash = _hash(text_value)
            item.embedded_at = now
            done += 1
        await session.flush()
    return done


# ── searching ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class Candidate:
    """One row the email might be about, and why it is being considered."""

    item: ProposalIndexItem
    #: Postgres' own ranking of the words. 0 when the row arrived by reference.
    text_rank: float = 0.0
    #: Cosine against the email, or None when nothing was embedded.
    similarity: float | None = None
    #: A reference number that matched exactly. The strongest signal there is,
    #: because nobody types a tender number by accident.
    matched_reference: str | None = None

    @property
    def score(self) -> float:
        """One number to order by, weighted by how much each signal is worth.

        An exact reference dominates deliberately: two rows whose words look
        alike are common, and two rows sharing a tender number are the same
        tender. The similarity leads the rest, because it is the part that
        understands wording; the text rank breaks its ties.
        """
        total = 0.0
        if self.matched_reference:
            total += 1.0
        if self.similarity is not None:
            total += 0.6 * self.similarity
        total += 0.2 * min(self.text_rank, 1.0)
        return total


def _cosine(query: Sequence[float], rows: list[Sequence[float]]) -> list[float]:
    """Cosine of one vector against many, in one matrix multiply.

    numpy rather than the database because ``pgvector`` cannot be created on
    this server. Over a few hundred candidates the difference is microseconds;
    the reason to move it into Postgres would be scale, not speed here.
    """
    import numpy as np

    if not rows:
        return []
    matrix = np.asarray(rows, dtype="float32")
    vector = np.asarray(query, dtype="float32")
    matrix_norms = np.linalg.norm(matrix, axis=1)
    query_norm = float(np.linalg.norm(vector))
    if query_norm == 0:
        return [0.0] * len(rows)
    # Guard the division rather than filtering: a zero-length row is a row that
    # failed to embed, and it should score nothing rather than raise.
    matrix_norms[matrix_norms == 0] = 1e-9
    return (matrix @ vector / (matrix_norms * query_norm)).tolist()


async def candidates(
    session: AsyncSession,
    *,
    query_text: str,
    references: Iterable[str] = (),
    limit: int = CANDIDATE_LIMIT,
    include_closed: bool = True,
) -> list[Candidate]:
    """Stage one: the rows worth looking at, by words and by reference.

    Two queries rather than one, unioned in Python, because they answer
    different questions and one must not crowd out the other. A tender number
    that matches exactly has to survive into the shortlist even when the words
    around it match nothing, which is exactly the email that says little more
    than "re: T-2291, please reopen".
    """
    found: dict[str, Candidate] = {}

    wanted = [normalise(r) for r in references if normalise(r)]
    if wanted:
        # Compared with the punctuation stripped from both sides, because the
        # list holds QT-1001, QT/1001 and QT 1001 for the same number.
        stripped = func.upper(
            func.regexp_replace(
                func.coalesce(ProposalIndexItem.quote_no, ""), r"[^A-Za-z0-9]", "", "g"
            )
        )
        rows = (
            await session.scalars(
                select(ProposalIndexItem)
                .where(ProposalIndexItem.deleted.is_(False), stripped.in_(wanted))
                .limit(limit)
            )
        ).all()
        for row in rows:
            found[row.item_id] = Candidate(item=row, matched_reference=row.quote_no)

        # A reference is often written only in the notes, so look there too —
        # but as a plain containment test, which is cheap and precise.
        like = [f"%{r}%" for r in references if r]
        if like:
            clauses = [
                or_(
                    ProposalIndexItem.title.ilike(pattern),
                    ProposalIndexItem.remarks.ilike(pattern),
                    ProposalIndexItem.working_notes.ilike(pattern),
                )
                for pattern in like
            ]
            rows = (
                await session.scalars(
                    select(ProposalIndexItem)
                    .where(ProposalIndexItem.deleted.is_(False), or_(*clauses))
                    .limit(limit)
                )
            ).all()
            for row in rows:
                found.setdefault(row.item_id, Candidate(item=row, matched_reference=None))

    words = (query_text or "").strip()
    if words:
        query = func.websearch_to_tsquery(cast("english", REGCONFIG), words)
        rank = func.ts_rank(ProposalIndexItem.search_vector, query)
        statement = (
            select(ProposalIndexItem, rank.label("rank"))
            .where(
                ProposalIndexItem.deleted.is_(False),
                ProposalIndexItem.search_vector.op("@@")(query),
            )
            .order_by(rank.desc())
            .limit(limit)
        )
        if not include_closed:
            statement = statement.where(ProposalIndexItem.is_open.is_(True))
        for row, ranked in (await session.execute(statement)).all():
            entry = found.get(row.item_id)
            if entry is None:
                found[row.item_id] = Candidate(item=row, text_rank=float(ranked or 0))
            else:
                entry.text_rank = float(ranked or 0)

    return list(found.values())


async def search(
    session: AsyncSession,
    embedder: Embedder,
    *,
    query_text: str,
    references: Iterable[str] = (),
    shortlist: int = SHORTLIST,
    include_closed: bool = True,
) -> list[Candidate]:
    """The shortlist an email should be adjudicated against.

    Stage one narrows the list to a few hundred by words and reference; stage
    two orders those by meaning. What comes back is small enough to hand to a
    model, and the model never sees the list.
    """
    pool = await candidates(
        session,
        query_text=query_text,
        references=references,
        include_closed=include_closed,
    )
    if not pool:
        return []

    embedded = [c for c in pool if c.item.embedding]
    if embedded and embedder.available:
        vectors = await embedder.embed([query_text])
        if vectors:
            scores = _cosine(vectors[0], [c.item.embedding or [] for c in embedded])
            for candidate, similarity in zip(embedded, scores, strict=False):
                candidate.similarity = float(similarity)

    pool.sort(key=lambda c: c.score, reverse=True)
    return pool[:shortlist]


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
        embedder: Embedder,
        *,
        embed: bool = True,
    ) -> SyncReport:
        async with self._lock:
            self.last = await sync(session, sharepoint, embedder, embed=embed)
            return self.last
