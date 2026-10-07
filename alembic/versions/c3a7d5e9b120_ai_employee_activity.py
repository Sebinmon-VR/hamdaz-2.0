"""ai employee teams activity

What an AI employee did with each Teams message it saw, the latest hundred,
for its card on the admin page. See app/msteams/worker.py.

Revision ID: c3a7d5e9b120
Revises: b6e2f9a4d851
Create Date: 2026-10-06 22:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c3a7d5e9b120'
down_revision: str | None = 'b6e2f9a4d851'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('ai_employee_accounts', sa.Column('activity', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('ai_employee_accounts', 'activity')
