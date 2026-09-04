"""quote negotiation rounds

Three things, all of one piece:

* ``quote_revisions`` — what a quote was when a round of it ended. The line
  items are replaced wholesale on every repricing, so without this the previous
  round is gone and a negotiation is an argument about numbers nobody can see.
* ``source_task_id`` / ``source_task_url`` — the Proposals row a quote was
  raised from, so the enquiry and the quote stay connected.
* the ``in_negotiation`` status needs no schema change: status is a string
  column, deliberately, so a new one is not a migration.

Revision ID: d7b4e02a15c9
Revises: c3a1f70b9d24
Create Date: 2026-09-04 13:05:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd7b4e02a15c9'
down_revision: str | None = 'c3a1f70b9d24'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('quote_requests', sa.Column('source_task_id', sa.String(length=120), nullable=True))
    op.add_column('quote_requests', sa.Column('source_task_url', sa.Text(), nullable=True))
    op.create_index(
        'ix_quote_requests_source_task_id', 'quote_requests', ['source_task_id']
    )

    op.create_table(
        'quote_revisions',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('request_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('outcome', sa.String(length=30), nullable=False),
        sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['request_id'], ['quote_requests.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_id', 'revision', name='uq_quote_revision_round'),
    )


def downgrade() -> None:
    op.drop_table('quote_revisions')
    op.drop_index('ix_quote_requests_source_task_id', table_name='quote_requests')
    op.drop_column('quote_requests', 'source_task_url')
    op.drop_column('quote_requests', 'source_task_id')
