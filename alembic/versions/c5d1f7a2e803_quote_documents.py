"""quote documents

Any document can now be uploaded against a quote request — the customer's RFQ,
the end user's PO, a courier quote, a datasheet — not only the supplier's
quotation, and every one of them is filed into the task's own folder in the
Proposal Team Channel library rather than kept in the database. This table is
what the system knows about each file: its kind, its link, who uploaded it,
and what was read out of it.

The quote itself remembers the folder its documents went into, so later uploads
and the selling & costing report land in the same place.

Revision ID: c5d1f7a2e803
Revises: b2c7e9d41f58
Create Date: 2026-09-25 10:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = 'c5d1f7a2e803'
down_revision: str | None = 'b2c7e9d41f58'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('quote_requests', sa.Column('drive_folder', sa.Text(), nullable=True))
    op.add_column('quote_requests', sa.Column('drive_folder_url', sa.Text(), nullable=True))
    op.add_column('quote_requests', sa.Column('filing_error', sa.Text(), nullable=True))

    op.create_table(
        'quote_documents',
        sa.Column(
            'id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'),
            nullable=False,
        ),
        sa.Column('request_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('file_name', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=True),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('drive_item_id', sa.String(length=120), nullable=True),
        sa.Column('drive_url', sa.Text(), nullable=True),
        sa.Column('drive_path', sa.Text(), nullable=True),
        sa.Column('uploaded_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('supplier_quote_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('revision', sa.Integer(), nullable=True),
        sa.Column('extracted', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('suggestions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['request_id'], ['quote_requests.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(
            ['supplier_quote_id'], ['supplier_quotes.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_quote_documents_request', 'quote_documents', ['request_id', 'kind'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_quote_documents_request', table_name='quote_documents')
    op.drop_table('quote_documents')
    op.drop_column('quote_requests', 'filing_error')
    op.drop_column('quote_requests', 'drive_folder_url')
    op.drop_column('quote_requests', 'drive_folder')
