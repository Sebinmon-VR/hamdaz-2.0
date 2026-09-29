"""ariba sign-in limit

Sign-ins counted per day, apart from visits: a visit on the saved session costs
no sign-in, and the day's sign-ins are capped (ARIBA_MAX_LOGINS_PER_DAY).

Revision ID: d2b5f8c1e694
Revises: c6e1a8d3f425
Create Date: 2026-09-29 23:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'd2b5f8c1e694'
down_revision: str | None = 'c6e1a8d3f425'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('ariba_state', sa.Column('logins_on', sa.Date(), nullable=True))
    op.add_column('ariba_state', sa.Column('logins_today', sa.Integer(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    op.drop_column('ariba_state', 'logins_today')
    op.drop_column('ariba_state', 'logins_on')
