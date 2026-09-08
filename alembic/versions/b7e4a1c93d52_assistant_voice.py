"""assistant: which voice reads answers aloud, and how it should sound

Revision ID: b7e4a1c93d52
Revises: a3f9c2d17b04
Create Date: 2026-09-07

Three columns on the settings row. ``voice_instructions`` is nullable on
purpose: an empty box means "use the shipped wording", not "read this flatly
with no steering", and those are different enough to be worth distinguishing.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e4a1c93d52"
down_revision: str | None = "a3f9c2d17b04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assistant_settings",
        sa.Column(
            "voice_model",
            sa.String(length=40),
            server_default=sa.text("'gpt-4o-mini-tts'"),
            nullable=False,
        ),
    )
    op.add_column(
        "assistant_settings",
        sa.Column(
            "voice",
            sa.String(length=24),
            server_default=sa.text("'cedar'"),
            nullable=False,
        ),
    )
    op.add_column(
        "assistant_settings",
        sa.Column("voice_instructions", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_settings", "voice_instructions")
    op.drop_column("assistant_settings", "voice")
    op.drop_column("assistant_settings", "voice_model")
