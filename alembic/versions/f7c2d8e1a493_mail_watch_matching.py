"""mail watch matching

Step 3: each new-tender or negotiation mail matched against the Proposals
list — the verdict (new, existing, stage change, duplicate mail, not found,
unsure), the task it landed on and that task as SharePoint had it, the rows
considered, and the earlier mail it repeats.

Revision ID: f7c2d8e1a493
Revises: e6b1c4a9d730
Create Date: 2026-10-22 19:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'f7c2d8e1a493'
down_revision: str | None = 'e6b1c4a9d730'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> tuple[sa.Column, ...]:
    """Fresh each call: a Column belongs to the one table it is added to."""
    return (
        sa.Column('verdict', sa.String(length=20), nullable=True),
        sa.Column('matched_item_id', sa.String(length=64), nullable=True),
        sa.Column('match_method', sa.String(length=20), nullable=True),
        sa.Column('match_score', sa.Float(), nullable=True),
        sa.Column('match_reason', sa.Text(), nullable=True),
        sa.Column(
            'match_candidates', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column('task', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('duplicate_of_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('matched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('match_error', sa.Text(), nullable=True),
    )


def upgrade() -> None:
    for column in _columns():
        op.add_column('watched_mails', column)
    op.create_foreign_key(
        'fk_watched_mails_duplicate_of', 'watched_mails', 'watched_mails',
        ['duplicate_of_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index('ix_watched_mails_ariba_doc', 'watched_mails', ['ariba_doc'])
    op.create_index('ix_watched_mails_matched_item_id', 'watched_mails', ['matched_item_id'])


def downgrade() -> None:
    op.drop_index('ix_watched_mails_matched_item_id', table_name='watched_mails')
    op.drop_index('ix_watched_mails_ariba_doc', table_name='watched_mails')
    op.drop_constraint('fk_watched_mails_duplicate_of', 'watched_mails', type_='foreignkey')
    for column in reversed(_columns()):
        op.drop_column('watched_mails', column.name)
