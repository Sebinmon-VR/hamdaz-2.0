"""ariba tenders

The Events list from the Ariba supplier portal — title, End Time, status — and
one row of state for the reader: the reused browser session, the watermark over
the Proposals mirror, and the last visit.

Revision ID: f7a2c9e4b183
Revises: e7b2d5a9c104
Create Date: 2026-09-29 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'f7a2c9e4b183'
down_revision: str | None = 'e7b2d5a9c104'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        'ariba_event',
        sa.Column('doc_id', sa.String(length=40), nullable=False),
        sa.Column('reference', sa.String(length=40), nullable=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('end_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        *_stamps(),
        sa.PrimaryKeyConstraint('doc_id'),
    )
    op.create_index('ix_ariba_event_reference', 'ariba_event', ['reference'])
    op.create_table(
        'ariba_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_state', postgresql.JSONB(), nullable=True),
        sa.Column('watermark', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_visit_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_result', sa.Text(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('paused_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('visits_on', sa.Date(), nullable=True),
        sa.Column('visits_today', sa.Integer(), server_default=sa.text('0'), nullable=False),
        *_stamps(),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('ariba_state')
    op.drop_index('ix_ariba_event_reference', table_name='ariba_event')
    op.drop_table('ariba_event')
