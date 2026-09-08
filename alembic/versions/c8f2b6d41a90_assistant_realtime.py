"""assistant: spoken conversation through the realtime API

Revision ID: c8f2b6d41a90
Revises: b7e4a1c93d52
Create Date: 2026-09-07

Three columns, and the third is the point. ``realtime_writes_enabled`` is
separate from the module write policy because voice mode confirms differently:
the server refuses a confirmable write and the *client* puts the question, where
the text chat parks the run server-side and nothing moves until a person
answers. An administrator should have to accept that weaker guarantee
explicitly rather than inherit it from a switch they set for the text chat.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8f2b6d41a90"
down_revision: str | None = "b7e4a1c93d52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assistant_settings",
        sa.Column(
            "realtime_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "assistant_settings",
        sa.Column(
            "realtime_model",
            sa.String(length=40),
            server_default=sa.text("'gpt-realtime-2.1'"),
            nullable=False,
        ),
    )
    op.add_column(
        "assistant_settings",
        sa.Column(
            "realtime_writes_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("assistant_settings", "realtime_writes_enabled")
    op.drop_column("assistant_settings", "realtime_model")
    op.drop_column("assistant_settings", "realtime_enabled")
