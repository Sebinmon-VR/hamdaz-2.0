"""mail watch orders destination

Order cards went to the test channel only, fixed in code. Now a switch on the
Mail watch page: the test channel (the default, as before) or wherever the
other cards go — the chosen channel or group chat.

Revision ID: c5e9a3f7d218
Revises: b7e3d1f5a924
Create Date: 2026-11-02 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c5e9a3f7d218'
down_revision: str | None = 'b7e3d1f5a924'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'mailwatch_teams',
        sa.Column('orders_test_only', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('mailwatch_teams', 'orders_test_only')
