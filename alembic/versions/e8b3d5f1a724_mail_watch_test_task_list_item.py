"""mail watch test task list item

Where a new tender's task was created in the Proposals Test list (Test site):
its item id and link, or why it could not be.

Revision ID: e8b3d5f1a724
Revises: d4f7a2c9e318
Create Date: 2026-10-23 15:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e8b3d5f1a724'
down_revision: str | None = 'd4f7a2c9e318'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('mailwatch_test_tasks', sa.Column('list_item_id', sa.String(length=20), nullable=True))
    op.add_column('mailwatch_test_tasks', sa.Column('list_item_url', sa.Text(), nullable=True))
    op.add_column('mailwatch_test_tasks', sa.Column('list_error', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('mailwatch_test_tasks', 'list_error')
    op.drop_column('mailwatch_test_tasks', 'list_item_url')
    op.drop_column('mailwatch_test_tasks', 'list_item_id')
