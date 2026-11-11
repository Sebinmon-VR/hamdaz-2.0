"""website enquiries

Messages from the contact form on hamdaz.com, with where the visit came from
(campaign tags, Google's click id) and what sales did with each. The module row
itself comes from the catalogue: run the seed after upgrading. See app/website.

Revision ID: b7e3d9a4c218
Revises: c4e8a2f6d913
Create Date: 2026-11-11 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b7e3d9a4c218'
down_revision: str | None = 'c4e8a2f6d913'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'website_enquiries',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=True),
        sa.Column('phone', sa.String(length=40), nullable=True),
        sa.Column('company', sa.String(length=200), nullable=True),
        sa.Column('topics', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('page', sa.String(length=500), nullable=True),
        sa.Column('landing', sa.String(length=500), nullable=True),
        sa.Column('referrer', sa.String(length=500), nullable=True),
        sa.Column('attribution', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='new', nullable=False),
        sa.Column('assigned_to_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('handled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('handled_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['assigned_to_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['handled_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_website_enquiries_email', 'website_enquiries', ['email'])
    op.create_index('ix_website_enquiries_status', 'website_enquiries', ['status'])
    op.create_index('ix_website_enquiries_assigned_to_id', 'website_enquiries', ['assigned_to_id'])
    # The list is always newest first.
    op.create_index('ix_website_enquiries_created_at', 'website_enquiries', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_website_enquiries_created_at', table_name='website_enquiries')
    op.drop_index('ix_website_enquiries_assigned_to_id', table_name='website_enquiries')
    op.drop_index('ix_website_enquiries_status', table_name='website_enquiries')
    op.drop_index('ix_website_enquiries_email', table_name='website_enquiries')
    op.drop_table('website_enquiries')
