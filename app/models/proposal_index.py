"""A local mirror of the SharePoint Proposals list, and how it is searched.

Everything that needs to *ask questions of* the Proposals list asks this table
instead. SharePoint remains the system of record and is never written to from
here; this is a read-through copy that exists because of what the list costs to
query.

**Why mirror at all.** The list cannot be grouped, counted or searched
server-side — ``$apply`` is accepted and silently ignored, ``$count`` is
unsupported — so every question means pulling the rows and answering them in
process. That is a couple of seconds a time. Doing it once every few minutes in
the background is invisible; doing it inside a request, or once per incoming
email, is the whole latency budget. So it is done once, here, and every reader
gets a local table with indexes.

**How a match is found, and why it is three stages.** Matching an email to a row
means matching "KOC Pumping Stn. Bid" to "Kuwait Oil Company — pumping station
upgrade", where the reference numbers also disagree. That needs a language
model somewhere, and a language model cannot read the whole list: at a thousand
rows that is tens of thousands of tokens per email, slower and *less* accurate,
because a model reading a long list picks worse.

So the funnel narrows before it gets expensive:

1. **Full-text search**, on ``search_vector`` with a GIN index. Postgres core —
   no extension — and it stays fast whatever the list grows to. Plus an exact
   hit on any reference number in the mail, which catches the case the words
   would miss. A few hundred candidates.
2. **Vector similarity**, in process, over *only those candidates*. This is what
   understands that "pumping station" and "Pumping Stn." are the same thing.
   Bounded by the candidate set rather than the list, so memory does not grow
   with the list.
3. **The model**, on the surviving handful, to say which one it actually is.

Embeddings are stored as JSON rather than in a vector column because
``azure.extensions`` is empty on this server, so ``pgvector`` cannot be created
without an infrastructure change. At this size that costs nothing — the cosine
is a small matrix multiply over the candidates. If the list ever reaches the
tens of thousands, allowlist ``vector`` and move ``embedding`` to a real vector
column with an HNSW index; nothing else in the funnel has to change.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped


class ProposalIndexItem(Base, Timestamped):
    """One row of the Proposals list as this app holds it.

    Keyed by the SharePoint item id, so a sync is an upsert and the two systems
    cannot drift into duplicate rows for the same task.
    """

    __tablename__ = "proposal_index"
    __table_args__ = (
        # The first stage of the funnel. GIN over the tsvector is what keeps
        # candidate selection flat as the list grows.
        Index(
            "ix_proposal_index_search",
            "search_vector",
            postgresql_using="gin",
        ),
        Index("ix_proposal_index_assigned", "assigned_lookup_id"),
        Index("ix_proposal_index_open", "is_open"),
        Index("ix_proposal_index_quote_no", "quote_no"),
        Index("ix_proposal_index_modified", "sp_modified_at"),
    )

    #: The SharePoint list item id.
    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str | None] = mapped_column(String(80))
    #: The status the row really has — a row with no status whose bid closed is
    #: read as expired. Derived on the way in so every reader agrees, rather
    #: than each one re-deriving it. See ``ProposalTask.effective_status``.
    effective_status: Mapped[str | None] = mapped_column(String(80))
    priority: Mapped[str | None] = mapped_column(String(40))
    end_user: Mapped[str | None] = mapped_column(String(300))
    quote_no: Mapped[str | None] = mapped_column(String(120))
    submission_status: Mapped[str | None] = mapped_column(String(80))
    current_type: Mapped[str | None] = mapped_column(String(80))
    order_status: Mapped[str | None] = mapped_column(String(80))
    negotiation: Mapped[str | None] = mapped_column(String(120))
    remarks: Mapped[str | None] = mapped_column(Text)
    working_notes: Mapped[str | None] = mapped_column(Text)

    start_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    bid_closing_date: Mapped[date | None] = mapped_column(Date)
    #: BCD falling back to DueDate — the date that actually decides whether
    #: something is late.
    deadline: Mapped[date | None] = mapped_column(Date)

    #: Who holds it, in SharePoint's own terms. The lookup id is what the
    #: workload counts key on; the name is for showing a person.
    assigned_lookup_id: Mapped[str | None] = mapped_column(String(40))
    assigned_name: Mapped[str | None] = mapped_column(String(200))
    #: Denormalised onto the row deliberately. Scoring matches SharePoint people
    #: to users by email — the only identifier both systems agree on — and
    #: resolving it at score time would mean a SharePoint call per recompute,
    #: which is the whole thing the mirror exists to avoid. The sync already
    #: reads the site's people list, so this costs nothing to fill.
    assigned_email: Mapped[str | None] = mapped_column(String(320), index=True)

    is_open: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    sp_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sp_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Everything worth matching on, joined into one string. Kept so the
    #: embedding and the tsvector are demonstrably built from the same words.
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Maintained by the application rather than a generated column, because
    #: the weighting differs by field — a title hit means more than a hit in
    #: somebody's working notes, and a generated column cannot express that
    #: without pinning the weights into a migration.
    search_vector: Mapped[Any | None] = mapped_column(TSVECTOR)

    #: The embedding of ``search_text``, and the hash of the text it was built
    #: from. The hash is the whole economy of the thing: a sync re-embeds only
    #: rows whose words actually changed, so a list that is mostly static costs
    #: nothing to keep current.
    embedding: Mapped[list[float] | None] = mapped_column(JSONB)
    text_hash: Mapped[str | None] = mapped_column(String(64))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Gone from the list. Kept rather than deleted so a report or an intake
    #: record that referred to it still resolves to something.
    deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<ProposalIndexItem {self.item_id} {self.title[:40]!r}>"


class MirrorState(Base, Timestamped):
    """How the last sync went. One row; ``id`` is fixed at 1.

    Kept in the database rather than in memory because the answer to "is this
    data current" has to survive a restart, and because on more than one
    instance it is how they agree on who synced last.
    """

    __tablename__ = "proposal_mirror_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    #: Graph's delta token, when delta is in use. Null means the next sync is a
    #: full one, which is also the honest state after any error we could not
    #: interpret — a full sweep is two seconds and always correct.
    delta_token: Mapped[str | None] = mapped_column(Text)

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Rows the last sync read, changed and re-embedded. Re-embedded is the one
    #: to watch: if it is near the row count every time, the text hash is not
    #: doing its job and the embedding bill is real.
    rows_read: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    rows_changed: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    rows_embedded: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: What went wrong last time, if anything. A sync that fails leaves the
    #: mirror stale rather than empty, so this is the only sign it happened.
    last_error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<MirrorState synced={self.last_sync_at}>"
