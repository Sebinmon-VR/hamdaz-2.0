"""mail watch group chat

Cards can go to a group chat instead of a team channel: where they go, the
chat, and the scopes the connected account granted (a refresh asks for
exactly those, so a connection made before chats keeps working).

Revision ID: a6d2f8c4b931
Revises: f3c7e9a1b852
Create Date: 2026-10-23 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a6d2f8c4b931'
down_revision: str | None = 'f3c7e9a1b852'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'mailwatch_teams',
        sa.Column('target_kind', sa.String(length=10), server_default=sa.text("'channel'"), nullable=False),
    )
    op.add_column('mailwatch_teams', sa.Column('chat_id', sa.String(length=200), nullable=True))
    op.add_column('mailwatch_teams', sa.Column('chat_name', sa.String(length=300), nullable=True))
    op.add_column('mailwatch_teams', sa.Column('granted_scopes', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('mailwatch_teams', 'granted_scopes')
    op.drop_column('mailwatch_teams', 'chat_name')
    op.drop_column('mailwatch_teams', 'chat_id')
    op.drop_column('mailwatch_teams', 'target_kind')
