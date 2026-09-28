"""followup end of day report

The day's overdue-task reasons, sent once at the closing time as a PDF and a
workbook. The settings live on the follow-up's own settings row; anybody still
unanswered at the closing time is marked no_response (a string status, so no
schema change for it).

Seeded for the trial: 18:00 India time, to sebin@hamdaz.com only, CEO role off.

Revision ID: d9a4c6e1b852
Revises: c5e2a9d4f713
Create Date: 2026-09-29 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd9a4c6e1b852'
down_revision: str | None = 'c5e2a9d4f713'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    add = lambda column: op.add_column('followup_settings', column)  # noqa: E731
    add(sa.Column('digest_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    add(sa.Column('digest_time', sa.String(length=5), server_default=sa.text("'18:00'"), nullable=False))
    add(sa.Column('digest_timezone', sa.String(length=64), server_default=sa.text("'Asia/Kolkata'"), nullable=False))
    add(sa.Column(
        'digest_recipients', postgresql.ARRAY(sa.String(length=320)),
        server_default=sa.text("'{}'::varchar[]"), nullable=False,
    ))
    add(sa.Column('digest_include_ceo', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    add(sa.Column(
        'digest_formats', postgresql.ARRAY(sa.String(length=8)),
        server_default=sa.text("'{pdf,xlsx}'::varchar[]"), nullable=False,
    ))
    add(sa.Column('digest_sender_email', sa.String(length=320), nullable=True))
    add(sa.Column('digest_last_sent_on', sa.Date(), nullable=True))
    add(sa.Column('digest_last_error', sa.Text(), nullable=True))
    # The trial: to Sebin only, until the CEO switch is turned on.
    op.execute(
        "UPDATE followup_settings SET digest_recipients = ARRAY['sebin@hamdaz.com']::varchar[] "
        "WHERE id = 1"
    )


def downgrade() -> None:
    for column in (
        'digest_last_error', 'digest_last_sent_on', 'digest_sender_email', 'digest_formats',
        'digest_include_ceo', 'digest_recipients', 'digest_timezone', 'digest_time',
        'digest_enabled',
    ):
        op.drop_column('followup_settings', column)
