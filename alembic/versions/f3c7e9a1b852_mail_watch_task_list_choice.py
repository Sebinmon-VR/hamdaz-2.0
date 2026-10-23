"""mail watch task list choice

Which list a new tender's task is created in — the Proposals Test list, the
real Proposals list, or none — chosen on the Mail watch page; and, per task,
the list its item went into.

Revision ID: f3c7e9a1b852
Revises: e8b3d5f1a724
Create Date: 2026-10-23 16:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'f3c7e9a1b852'
down_revision: str | None = 'e8b3d5f1a724'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The Proposals Test list on the Test site, where every item so far was made.
TEST_LIST = 'fb27f225-106f-4c8a-9925-d5b939511e05'


def upgrade() -> None:
    op.add_column(
        'mailwatch_teams',
        sa.Column('task_list', sa.String(length=10), server_default=sa.text("'test'"), nullable=False),
    )
    op.add_column('mailwatch_test_tasks', sa.Column('list_id', sa.String(length=64), nullable=True))
    op.execute(
        f"UPDATE mailwatch_test_tasks SET list_id = '{TEST_LIST}' WHERE list_item_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column('mailwatch_test_tasks', 'list_id')
    op.drop_column('mailwatch_teams', 'task_list')
