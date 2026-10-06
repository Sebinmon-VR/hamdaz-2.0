"""enquiry analysis

A pre-sales task's documents read into requirement lines, each with what our
history says of it and, for a new item, what the web says. One analysis per
task, its documents (no bytes: they live in the task folder) and its lines.
See app/enquiries.

Revision ID: e8b3c5d1f402
Revises: d2f7a1c8e590
Create Date: 2026-10-05 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e8b3c5d1f402'
down_revision: str | None = 'd2f7a1c8e590'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    ]


def _id() -> sa.Column:
    return sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False)


def upgrade() -> None:
    op.create_table(
        'enquiry_analyses',
        _id(),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('task_title', sa.Text(), nullable=False),
        sa.Column('end_user', sa.String(length=300), nullable=True),
        sa.Column('bid_closing_date', sa.Date(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='idle', nullable=False),
        sa.Column('stage', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('customer', sa.String(length=300), nullable=True),
        sa.Column('deadline', sa.String(length=40), nullable=True),
        sa.Column('conditions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('missing', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('run_notes', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('web_search', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('model', sa.String(length=60), nullable=True),
        sa.Column('input_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('output_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('cost_usd', sa.Numeric(10, 4), server_default=sa.text('0'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('drive_folder', sa.Text(), nullable=True),
        sa.Column('drive_folder_url', sa.Text(), nullable=True),
        sa.Column('report_pdf_url', sa.Text(), nullable=True),
        sa.Column('report_xlsx_url', sa.Text(), nullable=True),
        sa.Column('filing_error', sa.Text(), nullable=True),
        sa.Column('comparison_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('run_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(['comparison_id'], ['quote_comparisons.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['run_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id'),
    )
    op.create_table(
        'enquiry_documents',
        _id(),
        sa.Column('analysis_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('origin_key', sa.String(length=400), nullable=False),
        sa.Column('file_name', sa.String(length=255), nullable=False),
        sa.Column('path', sa.Text(), nullable=True),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('drive_item_id', sa.String(length=120), nullable=True),
        sa.Column('web_url', sa.Text(), nullable=True),
        sa.Column('kind', sa.String(length=30), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('supplier_quote_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('uploaded_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(['analysis_id'], ['enquiry_analyses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['supplier_quote_id'], ['supplier_quotes.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('analysis_id', 'origin_key'),
    )
    op.create_table(
        'enquiry_lines',
        _id(),
        sa.Column('analysis_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('part_number', sa.String(length=120), nullable=True),
        sa.Column('brand', sa.String(length=120), nullable=True),
        sa.Column('quantity', sa.Numeric(18, 4), nullable=True),
        sa.Column('unit', sa.String(length=40), nullable=True),
        sa.Column('specification', sa.Text(), nullable=True),
        sa.Column('source_document', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('history', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('suppliers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('web', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(['analysis_id'], ['enquiry_analyses.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_enquiry_lines_analysis_id', 'enquiry_lines', ['analysis_id'])


def downgrade() -> None:
    op.drop_index('ix_enquiry_lines_analysis_id', table_name='enquiry_lines')
    op.drop_table('enquiry_lines')
    op.drop_table('enquiry_documents')
    op.drop_table('enquiry_analyses')
