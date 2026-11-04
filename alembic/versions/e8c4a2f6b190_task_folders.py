"""task folders

Each new Proposals task's folder in the Proposal Team Channel, and which of its
list attachments have been copied into it. See app/proposals/folders.py.

Revision ID: e8c4a2f6b190
Revises: d7a1c4e9f302
Create Date: 2026-11-04 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e8c4a2f6b190'
down_revision: str | None = 'd7a1c4e9f302'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'task_folders',
        sa.Column('item_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('folder', sa.Text(), nullable=True),
        sa.Column('folder_id', sa.String(length=64), nullable=True),
        sa.Column('web_url', sa.Text(), nullable=True),
        sa.Column('how', sa.String(length=20), nullable=True),
        sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('tries', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('tried_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column(
            'files', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column('files_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('files_tried_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('files_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('item_id'),
    )


def downgrade() -> None:
    op.drop_table('task_folders')
