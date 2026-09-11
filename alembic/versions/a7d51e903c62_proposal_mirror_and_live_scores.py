"""a local mirror of the Proposals list, and a live score per person

Revision ID: a7d51e903c62
Revises: f2c94a6e80b3
Create Date: 2026-09-08

Both tables exist for the same reason: the Proposals list cannot be grouped,
counted or searched server-side, so every question about it means pulling every
row. Doing that once every few minutes in the background is invisible. Doing it
inside a request, or once per incoming email, is the whole latency budget.

``proposal_index`` is the read-through copy. Nothing writes to SharePoint from
it — the list is read, and only read. Two things on it are worth explaining:

* ``search_vector`` carries a GIN index and is the first stage of matching an
  email to a row. Postgres core full-text, deliberately: ``azure.extensions`` is
  empty on this server, so ``pg_trgm`` and ``pgvector`` cannot be created
  without an infrastructure change, and the built-in text search needs nothing.
* ``embedding`` is JSON rather than a vector column for the same reason. At this
  size the cosine is a small matrix multiply over a few hundred candidates and
  the difference is microseconds. If the list ever reaches tens of thousands,
  allowlist ``vector``, move this column, and nothing else has to change.

``live_scores`` answers "who should get the next proposal", which has exactly
one right answer at any moment — so it is a row per person, overwritten, rather
than another ``analytics_runs`` row. Recomputing on every change would otherwise
mean hundreds of runs a day that nobody made a decision from, burying the ones
that recorded a real assignment.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7d51e903c62"
down_revision: str | None = "f2c94a6e80b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "proposal_index",
        sa.Column("item_id", sa.String(length=64), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=80), nullable=True),
        sa.Column("effective_status", sa.String(length=80), nullable=True),
        sa.Column("priority", sa.String(length=40), nullable=True),
        sa.Column("end_user", sa.String(length=300), nullable=True),
        sa.Column("quote_no", sa.String(length=120), nullable=True),
        sa.Column("submission_status", sa.String(length=80), nullable=True),
        sa.Column("current_type", sa.String(length=80), nullable=True),
        sa.Column("order_status", sa.String(length=80), nullable=True),
        sa.Column("negotiation", sa.String(length=120), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("working_notes", sa.Text(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("bid_closing_date", sa.Date(), nullable=True),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("assigned_lookup_id", sa.String(length=40), nullable=True),
        sa.Column("assigned_name", sa.String(length=200), nullable=True),
        # Denormalised: scoring matches SharePoint people to users by email,
        # and resolving it at score time would mean a SharePoint call per
        # recompute -- the one thing the mirror exists to avoid.
        sa.Column("assigned_email", sa.String(length=320), nullable=True),
        sa.Column("is_open", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sp_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sp_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("embedding", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("text_hash", sa.String(length=64), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    # The first stage of the funnel, and the reason it stays flat as the list
    # grows. Everything after it works on a few hundred rows at most.
    op.create_index(
        "ix_proposal_index_search",
        "proposal_index",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index("ix_proposal_index_assigned", "proposal_index", ["assigned_lookup_id"])
    op.create_index("ix_proposal_index_assigned_email", "proposal_index", ["assigned_email"])
    op.create_index("ix_proposal_index_open", "proposal_index", ["is_open"])
    op.create_index("ix_proposal_index_quote_no", "proposal_index", ["quote_no"])
    op.create_index("ix_proposal_index_modified", "proposal_index", ["sp_modified_at"])

    op.create_table(
        "proposal_mirror_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("delta_token", sa.Text(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_full_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows_read", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_changed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_embedded", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("duration_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_stamps(),
    )

    op.create_table(
        "live_scores",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        # NULL is the organisation-wide ranking; a person may hold that row and
        # a row per team, because the least loaded person in presales is not
        # the least loaded person overall.
        sa.Column(
            "team_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("sharepoint_lookup_id", sa.String(length=40), nullable=True),
        sa.Column("total_tasks", sa.Integer(), nullable=False),
        sa.Column("open_tasks", sa.Integer(), nullable=False),
        sa.Column("active_tasks", sa.Integer(), nullable=False),
        sa.Column("completed_tasks", sa.Integer(), nullable=False),
        sa.Column("overdue_tasks", sa.Integer(), nullable=False),
        sa.Column("due_soon_tasks", sa.Integer(), nullable=False),
        sa.Column("no_status_tasks", sa.Integer(), nullable=False),
        sa.Column("days_since_assigned", sa.Integer(), nullable=True),
        sa.Column("capacity", sa.Numeric(5, 2), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "priority_score", sa.Numeric(8, 4), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("eligible", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("excluded_reason", sa.String(length=200), nullable=True),
        sa.Column(
            "factors", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("reason", sa.String(length=80), nullable=True),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        *_stamps(),
        sa.UniqueConstraint("user_id", "team_id", name="uq_live_score_user_team"),
    )
    op.create_index("ix_live_scores_team_rank", "live_scores", ["team_id", "rank"])
    op.create_index("ix_live_scores_rank", "live_scores", ["rank"])


def downgrade() -> None:
    # The tables go and their indexes go with them. Dropping the indexes by
    # name first would be tidier to read and would fail the moment this
    # revision is edited before it has reached every environment — which is
    # exactly what happened while it was being written.
    op.drop_table("live_scores")
    op.drop_table("proposal_mirror_state")
    op.drop_table("proposal_index")
