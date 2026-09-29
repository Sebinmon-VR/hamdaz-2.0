"""ariba sign-in block

A failed sign-in now stops sign-ins until a super admin resumes them, rather
than for a day: blocked_at and blocked_reason on the reader's state row.

Revision ID: c6e1a8d3f425
Revises: b4d7f2a9c318
Create Date: 2026-09-29 22:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c6e1a8d3f425'
down_revision: str | None = 'b4d7f2a9c318'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('ariba_state', sa.Column('blocked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('ariba_state', sa.Column('blocked_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('ariba_state', 'blocked_reason')
    op.drop_column('ariba_state', 'blocked_at')
