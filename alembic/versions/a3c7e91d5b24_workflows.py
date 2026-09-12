"""workflows: a team's process as arranged blocks, run one task at a time

Revision ID: a3c7e91d5b24
Revises: e2a9c74b1f38
Create Date: 2026-09-12

Six tables. ``workflows`` holds the definitions — an ordered list of blocks in
JSONB, versioned on every edit. ``workflow_runs`` is one execution for one
subject, carrying a copy of the steps it began on and everything it has learnt
in ``context``, so a run three weeks long survives an edit to its flow and a
restart of the process. ``workflow_run_events`` is the audit trail,
``workflow_run_files`` the documents a run holds (bytes in the row, as the
comparison module keeps supplier quotes), and ``workflow_run_messages`` every
mail a run sent or recognised as a reply.

``workflow_settings`` is one row of switches, all shipped off. Each gates a
class of side effect on the world outside this database — sending mail,
writing to SharePoint, creating in Zoho — so a freshly deployed flow prepares
everything and does nothing until an administrator has read what it prepared.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a3c7e91d5b24"
down_revision: str | None = "e2a9c74b1f38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("send_email", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("write_sharepoint", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("write_zoho", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("from_mailbox", sa.String(320), nullable=True),
        sa.Column("poll_seconds", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True),
        sa.Column("subject_kind", sa.String(40), server_default=sa.text("'proposal_task'"), nullable=False),
        sa.Column("trigger", sa.String(24), server_default=sa.text("'manual'"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workflows_team_id", "workflows", ["team_id"])

    op.create_table(
        "workflow_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("subject_kind", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.String(120), nullable=False),
        sa.Column("subject_label", sa.String(300), nullable=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tag", sa.String(16), nullable=False, unique=True),
        sa.Column("status", sa.String(24), server_default=sa.text("'running'"), nullable=False),
        sa.Column("step_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("pending", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancelled_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"])
    op.create_index("ix_workflow_runs_team_id", "workflow_runs", ["team_id"])
    op.create_index("ix_workflow_runs_subject", "workflow_runs", ["subject_kind", "subject_id"])
    op.create_index("ix_workflow_runs_status_wake", "workflow_runs", ["status", "wake_at"])
    op.create_index("ix_workflow_runs_owner_started", "workflow_runs", ["owner_id", "started_at"])

    op.create_table(
        "workflow_run_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("step_key", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workflow_run_events_run_seq", "workflow_run_events", ["run_id", "seq"], unique=True)

    op.create_table(
        "workflow_run_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_key", sa.String(64), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=True),
        sa.Column("size", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("origin", sa.String(300), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workflow_run_files_run_id", "workflow_run_files", ["run_id"])

    op.create_table(
        "workflow_run_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_key", sa.String(64), nullable=True),
        sa.Column("direction", sa.String(4), nullable=False),
        sa.Column("party", sa.String(300), nullable=True),
        sa.Column("address", sa.String(320), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("intake_message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("intake_messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workflow_run_messages_run_id", "workflow_run_messages", ["run_id"])


def downgrade() -> None:
    op.drop_table("workflow_run_messages")
    op.drop_table("workflow_run_files")
    op.drop_table("workflow_run_events")
    op.drop_table("workflow_runs")
    op.drop_table("workflows")
    op.drop_table("workflow_settings")
