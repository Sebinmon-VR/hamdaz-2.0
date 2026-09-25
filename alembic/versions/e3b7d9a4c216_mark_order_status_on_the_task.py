"""intake: set the matched task's OrderStatus when a purchase order arrives

Revision ID: e3b7d9a4c216
Revises: a7f3c9d2e604
Create Date: 2026-09-23

A purchase order never creates a task — the work was quoted, so it exists and
somebody holds it. Until now the intake found that task and told its holder,
and left the list alone. The team records an order by hand in the list's
``OrderStatus`` column (13 of 1,412 rows say Received today, one says Awaited),
and that is what a report or a flow reads to know an order came in; so the
intake can now set it.

``update_order_status`` is its own switch, separate from ``update_negotiation``
and ``create_in_sharepoint``, and ships off like both. An administrator may
reasonably want to be told about orders for a while before letting this mark
the live list. ``order_status_value`` is what gets written — a setting, because
the column's choices belong to the list.

No new column on ``intake_messages``: the change that would be made lands in
the existing ``would_update``, as the negotiation mark does.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3b7d9a4c216"
down_revision: str | None = "a7f3c9d2e604"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intake_settings",
        sa.Column(
            "update_order_status", sa.Boolean(),
            server_default=sa.text("false"), nullable=False,
        ),
    )
    op.add_column(
        "intake_settings",
        sa.Column(
            "order_status_value", sa.String(length=80),
            server_default=sa.text("'Received'"), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("intake_settings", "order_status_value")
    op.drop_column("intake_settings", "update_order_status")
