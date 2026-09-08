"""assistant: settings, models, policies, access rules, conversations, runs

Revision ID: a3f9c2d17b04
Revises: d4e2b8c1f077
Create Date: 2026-09-07

Written by hand. Two things are deliberate here:

* ``assistant_runs.user_id`` is a second foreign key to users alongside the one
  reached through the conversation, so cost per person is one index scan rather
  than a join through conversations on every analytics query.
* Events and messages cascade from their run and conversation, but a message
  only *nulls* its run reference: the chat transcript must survive a run row
  being deleted for any reason, or a person loses their history to housekeeping.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a3f9c2d17b04"
down_revision: str | None = "d4e2b8c1f077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
    ]


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True),
                     server_default=sa.text("gen_random_uuid()"), nullable=False)


def upgrade() -> None:
    # ── configuration ──────────────────────────────────────────────────
    op.create_table(
        "assistant_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("model_key", sa.String(length=64), server_default=sa.text("'gpt-5.6-sol'"),
                  nullable=False),
        sa.Column("reasoning_effort", sa.String(length=16), server_default=sa.text("'low'"),
                  nullable=False),
        sa.Column("max_tool_rounds", sa.Integer(), server_default=sa.text("8"), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), server_default=sa.text("4000"),
                  nullable=False),
        sa.Column("history_window", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("turns_per_user_per_hour", sa.Integer(), server_default=sa.text("60"),
                  nullable=False),
        sa.Column("daily_cost_cap_user_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("daily_cost_cap_total_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("audience_mode", sa.String(length=16), server_default=sa.text("'allow_list'"),
                  nullable=False),
        sa.Column("confirm_writes_default", sa.Boolean(), server_default=sa.text("true"),
                  nullable=False),
        sa.Column("voice_enabled", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
        sa.Column("extra_instructions", sa.Text(), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "assistant_models",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_price", sa.Numeric(10, 4), nullable=False),
        sa.Column("cached_input_price", sa.Numeric(10, 4), nullable=False),
        sa.Column("output_price", sa.Numeric(10, 4), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "assistant_module_policies",
        sa.Column("module_key", sa.String(length=40), nullable=False),
        sa.Column("read_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("write_enabled", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
        sa.Column("confirm_writes", sa.Boolean(), nullable=True),
        sa.Column("allowed_roles", postgresql.ARRAY(sa.String(length=40)), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("module_key"),
    )

    op.create_table(
        "assistant_tool_policies",
        sa.Column("tool_key", sa.String(length=80), nullable=False),
        sa.Column("module_key", sa.String(length=40), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("confirm_override", sa.Boolean(), nullable=True),
        sa.Column("allowed_roles", postgresql.ARRAY(sa.String(length=40)), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("tool_key"),
    )
    op.create_index("ix_assistant_tool_policies_module_key", "assistant_tool_policies",
                    ["module_key"])

    op.create_table(
        "assistant_access_rules",
        _uuid_pk(),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=120), nullable=False),
        sa.Column("subject_label", sa.String(length=200), nullable=False),
        sa.Column("effect", sa.String(length=8), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_rule_subject", "assistant_access_rules",
                    ["subject_type", "subject_id"])

    # ── record ─────────────────────────────────────────────────────────
    op.create_table(
        "assistant_conversations",
        _uuid_pk(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_conversations_user_id", "assistant_conversations",
                    ["user_id"])

    op.create_table(
        "assistant_runs",
        _uuid_pk(),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_text", sa.Text(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), server_default=sa.text("0"),
                  nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), server_default=sa.text("0"),
                  nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("tool_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rounds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
        sa.Column("transcript", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("pending", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["conversation_id"], ["assistant_conversations.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_runs_conversation_id", "assistant_runs", ["conversation_id"])
    op.create_index("ix_assistant_runs_user_started", "assistant_runs",
                    ["user_id", "started_at"])
    op.create_index("ix_assistant_runs_status", "assistant_runs", ["status"])

    op.create_table(
        "assistant_run_events",
        _uuid_pk(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("tool_key", sa.String(length=80), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["run_id"], ["assistant_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_events_run_seq", "assistant_run_events", ["run_id", "seq"])
    op.create_index("ix_assistant_run_events_kind", "assistant_run_events", ["kind"])
    op.create_index("ix_assistant_run_events_tool_key", "assistant_run_events", ["tool_key"])

    op.create_table(
        "assistant_messages",
        _uuid_pk(),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["conversation_id"], ["assistant_conversations.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["assistant_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_messages_conv_seq", "assistant_messages",
                    ["conversation_id", "seq"])


def downgrade() -> None:
    op.drop_table("assistant_messages")
    op.drop_table("assistant_run_events")
    op.drop_table("assistant_runs")
    op.drop_table("assistant_conversations")
    op.drop_table("assistant_access_rules")
    op.drop_table("assistant_tool_policies")
    op.drop_table("assistant_module_policies")
    op.drop_table("assistant_models")
    op.drop_table("assistant_settings")
