"""reports: a manager's brief on each one, and a chat that knows which one

Revision ID: e2a9c74b1f38
Revises: d9f3b28c47e1
Create Date: 2026-09-11

A filled-in report is long by design — six sections, every task, every issue,
every metric, plus whatever the team's template adds on top. That is the right
shape for the record and the wrong shape for a manager with nine of them to
read on a Monday, which is the complaint this answers.

The brief is a short account of one report, written by the assistant's model
from that report's own contents and stored on the report. Stored, not computed
per view: a submitted report never changes, so its brief is written once and
read many times, and the columns are what make that true. ``brief_input_hash``
is what makes it *honest* — a brief whose report has since been edited is
detectable rather than merely trusted.

``report_settings`` gains the switches, because when a brief gets written is an
administrator's decision and not a developer's. It is off by default and
deliberately so: this is the only part of the module that spends money per
report, and that should start with somebody turning it on rather than with an
invoice.

The three columns on ``assistant_conversations`` are what lets the box on a
report page be the assistant rather than an imitation of one. A conversation
can now say what it is about, so a follow-up question arrives already knowing
which report it concerns. The pair is deliberately keyless: a conversation
outlives its subject, and deleting a report should not take the conversation
about it with it.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e2a9c74b1f38"
down_revision: str | None = "d9f3b28c47e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── the switches ───────────────────────────────────────────────────
    op.add_column(
        "report_settings",
        sa.Column(
            "brief_enabled", sa.Boolean(),
            server_default=sa.text("false"), nullable=False,
        ),
    )
    op.add_column(
        "report_settings",
        sa.Column(
            "brief_mode", sa.String(length=16),
            server_default=sa.text("'on_submit'"), nullable=False,
        ),
    )
    op.add_column(
        "report_settings",
        sa.Column(
            "brief_followup", sa.String(length=16),
            server_default=sa.text("'chat'"), nullable=False,
        ),
    )
    op.add_column(
        "report_settings", sa.Column("brief_model_key", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "report_settings",
        sa.Column(
            "brief_max_words", sa.Integer(),
            server_default=sa.text("180"), nullable=False,
        ),
    )

    # ── the brief itself ───────────────────────────────────────────────
    #
    # Note the name. ``reports.summary`` already exists and is the *author's*
    # own section of the form; these are the assistant's account of the whole
    # thing. Neither is renamed, because a report filed last month has to keep
    # meaning what it meant.
    op.add_column("reports", sa.Column("brief_headline", sa.String(length=300), nullable=True))
    op.add_column("reports", sa.Column("brief", sa.Text(), nullable=True))
    op.add_column("reports", sa.Column("brief_model", sa.String(length=64), nullable=True))
    op.add_column(
        "reports",
        sa.Column("brief_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("reports", sa.Column("brief_input_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "reports",
        sa.Column("brief_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("reports", sa.Column("brief_error", sa.Text(), nullable=True))
    op.add_column(
        "reports",
        sa.Column("brief_tokens_in", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "reports",
        sa.Column("brief_tokens_out", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )

    # ── what a chat is about ───────────────────────────────────────────
    op.add_column(
        "assistant_conversations", sa.Column("subject_kind", sa.String(length=24), nullable=True)
    )
    op.add_column(
        "assistant_conversations",
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "assistant_conversations",
        sa.Column("subject_label", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "ix_assistant_conversations_subject",
        "assistant_conversations",
        ["subject_kind", "subject_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_conversations_subject", table_name="assistant_conversations")
    for column in ("subject_label", "subject_id", "subject_kind"):
        op.drop_column("assistant_conversations", column)

    for column in (
        "brief_tokens_out",
        "brief_tokens_in",
        "brief_error",
        "brief_revision",
        "brief_input_hash",
        "brief_generated_at",
        "brief_model",
        "brief",
        "brief_headline",
    ):
        op.drop_column("reports", column)

    for column in (
        "brief_max_words",
        "brief_model_key",
        "brief_followup",
        "brief_mode",
        "brief_enabled",
    ):
        op.drop_column("report_settings", column)
