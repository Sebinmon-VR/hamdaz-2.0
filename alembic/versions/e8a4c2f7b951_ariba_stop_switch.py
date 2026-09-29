"""ariba stop switch

A super admin can stop the Ariba reader from the admin page — no visits, no
BCD corrections — and start it again. Who and when, on the state row.

Revision ID: e8a4c2f7b951
Revises: d2b5f8c1e694
Create Date: 2026-09-30 09:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e8a4c2f7b951'
down_revision: str | None = 'd2b5f8c1e694'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('ariba_state', sa.Column('stopped_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('ariba_state', sa.Column('stopped_by', sa.String(length=320), nullable=True))


def downgrade() -> None:
    op.drop_column('ariba_state', 'stopped_by')
    op.drop_column('ariba_state', 'stopped_at')
