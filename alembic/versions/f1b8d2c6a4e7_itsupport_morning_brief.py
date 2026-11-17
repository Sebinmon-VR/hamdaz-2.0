"""it support morning brief and web sign-in

The morning brief's switch (off) and time (09:30 IST) on IT Support's
settings row (app/itsupport/brief.py), and the IT Support account's sign-in
made with Connect on the settings page, kept encrypted so the server running
the agent has it (app/itsupport/graph.py).

Revision ID: f1b8d2c6a4e7
Revises: e5c1a7d3b902
Create Date: 2026-11-17 15:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = 'f1b8d2c6a4e7'
down_revision: str | None = 'e5c1a7d3b902'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('itsupport_settings', sa.Column(
        'brief_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('itsupport_settings', sa.Column(
        'brief_at', sa.String(length=5), server_default='09:30', nullable=False))
    op.add_column('itsupport_settings', sa.Column('sign_in', postgresql.JSONB(), nullable=True))
    op.add_column('itsupport_settings', sa.Column(
        'connected_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('itsupport_settings', sa.Column(
        'connected_by_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key('fk_itsupport_settings_connected_by', 'itsupport_settings', 'users',
                          ['connected_by_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_itsupport_settings_connected_by', 'itsupport_settings',
                       type_='foreignkey')
    op.drop_column('itsupport_settings', 'connected_by_id')
    op.drop_column('itsupport_settings', 'connected_at')
    op.drop_column('itsupport_settings', 'sign_in')
    op.drop_column('itsupport_settings', 'brief_at')
    op.drop_column('itsupport_settings', 'brief_enabled')
