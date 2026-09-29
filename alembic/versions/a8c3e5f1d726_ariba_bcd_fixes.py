"""ariba bcd fixes

Each Proposals row whose "BCD UAE Time" disagreed with the Ariba End Time, and
whether it was corrected — a preview while ARIBA_FIX_BCD is off, the record of
what was written once it is on.

Revision ID: a8c3e5f1d726
Revises: f7a2c9e4b183
Create Date: 2026-09-29 20:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a8c3e5f1d726'
down_revision: str | None = 'f7a2c9e4b183'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'ariba_bcd_fix',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('item_id', sa.String(length=64), nullable=False),
        sa.Column('doc_id', sa.String(length=40), nullable=False),
        sa.Column('reference', sa.String(length=40), nullable=False),
        sa.Column('task_title', sa.Text(), nullable=False),
        sa.Column('old_bcd', sa.String(length=40), nullable=True),
        sa.Column('new_bcd', sa.String(length=40), nullable=False),
        sa.Column('ariba_end_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('applied', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ariba_bcd_fix_item_id', 'ariba_bcd_fix', ['item_id'])


def downgrade() -> None:
    op.drop_index('ix_ariba_bcd_fix_item_id', table_name='ariba_bcd_fix')
    op.drop_table('ariba_bcd_fix')
