"""intake: mark the matched task when a negotiation arrives

Revision ID: c4a71b6de902
Revises: b8e4f27a15d3
Create Date: 2026-09-08

A negotiation never creates a task — the work already exists and somebody holds
it — so nothing changes in SharePoint on its own, and a Power Automate flow
triggered by "an item was created or modified" has nothing to react to. Setting
the matched task's ``Negotiation`` column gives it one.

The column is a choice of Yes and No, and today 9 of 1,326 rows carry Yes, so a
flow triggering on it will be clean rather than firing on half the list.

``update_negotiation`` is its own switch, separate from
``create_in_sharepoint``, and ships off like it. They are different acts:
marking a column on a row that already exists is a much smaller thing than
creating a row and assigning it to a person, and an administrator may
reasonably want one without the other.

``would_update`` is the counterpart of ``would_create`` — the change that would
be made, recorded whether or not it was sent, so the switched-off mode is still
inspectable rather than silent.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4a71b6de902"
down_revision: str | None = "b8e4f27a15d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intake_settings",
        sa.Column(
            "update_negotiation", sa.Boolean(),
            server_default=sa.text("false"), nullable=False,
        ),
    )
    op.add_column(
        "intake_settings",
        sa.Column(
            "negotiation_value", sa.String(length=60),
            server_default=sa.text("'Yes'"), nullable=False,
        ),
    )
    op.add_column(
        "intake_messages",
        sa.Column("would_update", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("intake_messages", "would_update")
    op.drop_column("intake_settings", "negotiation_value")
    op.drop_column("intake_settings", "update_negotiation")
