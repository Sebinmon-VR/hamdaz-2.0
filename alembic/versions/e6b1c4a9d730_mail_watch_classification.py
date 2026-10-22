"""mail watch classification

Step 2: each watched mail's category (new tender, negotiation, general,
confidential), stage, and the details read out of it — reference numbers, the
Ariba document number, customer, closing date — for step 3 to match on.

Revision ID: e6b1c4a9d730
Revises: d3a8f1c6e527
Create Date: 2026-10-22 16:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e6b1c4a9d730'
down_revision: str | None = 'd3a8f1c6e527'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> tuple[sa.Column, ...]:
    """Fresh each call: a Column belongs to the one table it is added to."""
    return (
        sa.Column('category', sa.String(length=20), nullable=True),
        sa.Column('kind', sa.String(length=30), nullable=True),
        sa.Column('stage', sa.String(length=20), nullable=True),
        sa.Column('decided_by', sa.String(length=10), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('customer', sa.String(length=200), nullable=True),
        sa.Column(
            'reference_numbers', postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"), nullable=False,
        ),
        sa.Column('ariba_doc', sa.String(length=30), nullable=True),
        sa.Column('closing_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('original_sender', sa.String(length=320), nullable=True),
        sa.Column('model_cost_usd', sa.Numeric(10, 5), server_default=sa.text('0'), nullable=False),
        sa.Column('classified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('classify_error', sa.Text(), nullable=True),
    )


def upgrade() -> None:
    for column in _columns():
        op.add_column('watched_mails', column)
    op.create_index('ix_watched_mails_category', 'watched_mails', ['category'])


def downgrade() -> None:
    op.drop_index('ix_watched_mails_category', table_name='watched_mails')
    for column in reversed(_columns()):
        op.drop_column('watched_mails', column.name)
