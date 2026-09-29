"""ariba participated

The Events list's Participated column on each event, so a task marked Submitted
whose tender Ariba shows no response for can be flagged while there is time.

Revision ID: b4d7f2a9c318
Revises: a8c3e5f1d726
Create Date: 2026-09-29 21:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'b4d7f2a9c318'
down_revision: str | None = 'a8c3e5f1d726'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('ariba_event', sa.Column('participated', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('ariba_event', 'participated')
