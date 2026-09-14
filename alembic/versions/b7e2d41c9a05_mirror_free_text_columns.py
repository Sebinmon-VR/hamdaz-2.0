"""proposal mirror: free-text columns, the list subscription, labels on live rows

Revision ID: b7e2d41c9a05
Revises: a3c7e91d5b24
Create Date: 2026-09-14

The first sync against the live list failed on its first pass: somebody had
written a note into the Quote No column ("Contacted webqem — they declined
due to short notice...", 183 characters) and the mirror held it as
``varchar(120)``. A mirror that refuses a whole row over one long note would
silently undercount that person's load, and the list is free text wherever
SharePoint lets people type. Both columns become ``text``.

Two more things the live path needed at the same time. ``proposal_mirror_state``
gains the Graph subscription on the Proposals list, so a row edited in
SharePoint reaches the ranking in seconds instead of at the next timer tick.
``live_scores`` gains the labels each row was scored with, so publishing the
standing to the ``useranalytics`` list does not have to recompute them.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7e2d41c9a05"
down_revision: str | None = "a3c7e91d5b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("proposal_index", "quote_no", type_=sa.Text(), existing_type=sa.String(120))
    op.alter_column(
        "proposal_index", "negotiation", type_=sa.Text(), existing_type=sa.String(120)
    )
    op.add_column(
        "proposal_mirror_state", sa.Column("subscription_id", sa.String(120), nullable=True)
    )
    op.add_column(
        "proposal_mirror_state",
        sa.Column("subscription_secret", sa.String(120), nullable=True),
    )
    op.add_column(
        "proposal_mirror_state",
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "live_scores",
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("live_scores", "labels")
    op.drop_column("proposal_mirror_state", "subscription_expires_at")
    op.drop_column("proposal_mirror_state", "subscription_secret")
    op.drop_column("proposal_mirror_state", "subscription_id")
    # Values longer than 120 would be refused; there were none before this.
    op.alter_column(
        "proposal_index", "negotiation", type_=sa.String(120), existing_type=sa.Text()
    )
    op.alter_column("proposal_index", "quote_no", type_=sa.String(120), existing_type=sa.Text())
