"""approver notifications

Whether the approvers were told a quote is waiting, and what stopped it if they
were not. Recorded on the row rather than only logged: "did they get told?" is
a question somebody asks about one specific quote, usually on the day it
matters, and a log line is the wrong place to answer it from.

Revision ID: e81c5d3f6a72
Revises: d7b4e02a15c9
Create Date: 2026-09-04 15:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e81c5d3f6a72'
down_revision: str | None = 'd7b4e02a15c9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'quote_requests',
        sa.Column('approvers_notified_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column('quote_requests', sa.Column('notify_error', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('quote_requests', 'notify_error')
    op.drop_column('quote_requests', 'approvers_notified_at')
