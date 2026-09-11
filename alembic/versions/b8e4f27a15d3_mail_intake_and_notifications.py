"""mail intake, and notifications

Revision ID: b8e4f27a15d3
Revises: a7d51e903c62
Create Date: 2026-09-08

Work arrives by email — a tender forwarded by the CEO — and today somebody
reads it, works out whether it is already in the Proposals list, finds who
should take it and types it in. These tables are that judgement written down.

``intake_settings`` is one row of switches. The one that matters is
``create_in_sharepoint``, and it ships **false**: the Proposals list is live and
the team works in it, so until a super admin deliberately turns this on, a
message that would raise a task records the exact payload it would have posted
and posts nothing. That is not a test mode — it is how this runs until its
judgement has been watched for a while.

``intake_messages`` keeps every message, including the ones it ignored. A
pipeline that only records what it acted on cannot answer the question people
actually ask of it, which is "why did nothing happen when I sent that".

``notifications`` is deliberately generic. The intake is the first thing to
raise one, but "a tender was assigned to you" and "your leave was approved" are
the same shape of fact, and a notification table per feature is how an
application ends up with four bells in the corner of the screen. In-app is the
record; the Teams copy is a copy, so a misconfigured webhook loses a duplicate
rather than the only trace.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b8e4f27a15d3"
down_revision: str | None = "a7d51e903c62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    ]


def _pk() -> sa.Column:
    return sa.Column(
        "id", postgresql.UUID(as_uuid=True), primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def upgrade() -> None:
    op.create_table(
        "intake_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("mailbox", sa.String(length=320), server_default=sa.text("''"), nullable=False),
        # Empty admits nobody. The opposite of the usual convention, and the
        # point: an unconfigured intake must not read every message in a
        # mailbox and create tasks from them.
        sa.Column(
            "allowed_senders", postgresql.ARRAY(sa.String(length=320)),
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
        sa.Column(
            "allowed_domains", postgresql.ARRAY(sa.String(length=200)),
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
        # The live-write switch. Ships off.
        sa.Column(
            "create_in_sharepoint", sa.Boolean(),
            server_default=sa.text("false"), nullable=False,
        ),
        sa.Column(
            "assign_team_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "match_threshold", sa.Numeric(3, 2),
            server_default=sa.text("0.70"), nullable=False,
        ),
        sa.Column(
            "classify_threshold", sa.Numeric(3, 2),
            server_default=sa.text("0.60"), nullable=False,
        ),
        sa.Column("teams_webhook_url", sa.Text(), nullable=True),
        sa.Column("notify_in_app", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notify_teams", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("poll_seconds", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("delta_link", sa.Text(), nullable=True),
        sa.Column("watch_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("subscription_id", sa.String(length=120), nullable=True),
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("subscription_secret", sa.String(length=120), nullable=True),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_by_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        *_stamps(),
    )

    op.create_table(
        "intake_messages",
        _pk(),
        sa.Column("graph_message_id", sa.String(length=512), nullable=False),
        sa.Column("conversation_id", sa.String(length=512), nullable=True),
        sa.Column("internet_message_id", sa.String(length=998), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sender_email", sa.String(length=320), nullable=True),
        sa.Column("sender_name", sa.String(length=200), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "has_attachments", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("web_link", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=16),
            server_default=sa.text("'received'"), nullable=False,
        ),
        sa.Column("category", sa.String(length=24), nullable=True),
        sa.Column(
            "is_reopened", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column(
            "extracted", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("matched_item_id", sa.String(length=64), nullable=True),
        sa.Column("match_confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("match_reason", sa.Text(), nullable=True),
        sa.Column(
            "candidates", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column(
            "action", sa.String(length=24), server_default=sa.text("'none'"), nullable=False
        ),
        sa.Column(
            "assigned_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("assigned_reason", sa.String(length=200), nullable=True),
        sa.Column("created_item_id", sa.String(length=64), nullable=True),
        # Exactly what would be posted to SharePoint, field for field. With
        # writing switched off, this is the whole output.
        sa.Column("would_create", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "notified_user_ids", postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'::varchar[]"), nullable=False,
        ),
        sa.Column(
            "notified_teams", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        *_stamps(),
    )
    # Graph delivers the same message twice — a notification and a poll racing,
    # or a retried webhook. This is what makes that a no-op instead of two tasks.
    op.create_index(
        "uq_intake_message_graph_id", "intake_messages", ["graph_message_id"], unique=True
    )
    op.create_index("ix_intake_messages_received", "intake_messages", ["received_at"])
    op.create_index("ix_intake_messages_status", "intake_messages", ["status"])
    op.create_index("ix_intake_messages_category", "intake_messages", ["category"])
    op.create_index("ix_intake_messages_sender", "intake_messages", ["sender_email"])
    op.create_index("ix_intake_messages_conversation", "intake_messages", ["conversation_id"])
    op.create_index("ix_intake_messages_matched", "intake_messages", ["matched_item_id"])

    op.create_table(
        "notifications",
        _pk(),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=True),
        sa.Column("source_id", sa.String(length=120), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "sent_to_teams", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        *_stamps(),
    )
    op.create_index("ix_notifications_user_created", "notifications", ["user_id", "created_at"])
    op.create_index("ix_notifications_unread", "notifications", ["user_id", "read_at"])
    op.create_index("ix_notifications_kind", "notifications", ["kind"])
    # Partial, so notifications that name nothing can repeat freely while the
    # ones that do are raised at most once per person.
    op.create_index(
        "uq_notification_dedupe",
        "notifications",
        ["user_id", "source", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("intake_messages")
    op.drop_table("intake_settings")
