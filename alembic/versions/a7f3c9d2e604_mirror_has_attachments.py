"""carry the attachment flag on the mirrored Proposals row

The quoting screen now reads the caller's enquiries from the mirror rather than
from the list, because the list client truncates and the ceiling fell on exactly
the rows people were looking for. Everything that screen shows was already on
the mirrored row except one field: whether SharePoint holds files against the
item. Without it a row served from here would have said "no files" when it meant
"I was never told", which is the kind of wrong that nobody reports.

Not nullable and defaulted false, so existing rows are valid immediately. The
real values arrive on the next sync — every sync is a full read of the list, so
that is a minute at most and needs nothing run by hand.

Revision ID: a7f3c9d2e604
Revises: d1b6e3f97a24
Create Date: 2026-09-18 07:45:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a7f3c9d2e604'
down_revision: str | None = 'd1b6e3f97a24'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proposal_index",
        sa.Column(
            "has_attachments",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("proposal_index", "has_attachments")
