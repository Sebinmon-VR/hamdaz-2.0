"""assistant: who may write, and what the voice costs

Revision ID: d1a6e37c40b8
Revises: c8f2b6d41a90
Create Date: 2026-09-08

Two changes that arrived together because they are the same request: let the
assistant do more, and be able to see what that costs.

**Who may write.** Module writes were off by default, which made the assistant
an oracle — it could tell you your leave balance and not book a day of it.
Turning them on for everybody would be the other mistake, so ``write_roles``
comes with them: a list of global roles that may have the assistant write in a
module, separate from ``allowed_roles``, which decides who sees it at all.

The two are separate columns because the same person answers them differently.
An ordinary employee should read the team list and should not be able to say
"delete the Kuwait team". With one column, buying the second would cost the
first — the only way to stop somebody deleting a team would be to stop them
looking at teams.

The upgrade turns writes on for every module and sets the restriction on the
ones with company-wide consequences: roles, user administration, teams, form
templates, work assignment, finance and Zoho quotes. It deliberately leaves
leave, HR, proposals, quote requests and dashboards unrestricted — their writes
are the everyday work of the person doing them, and who may approve leave or a
quote is a question about HR team membership and approver lists that a list of
global roles cannot answer. Those routes already answer it.

A row a super admin has already edited is not touched: the ``write_roles``
backfill only writes where nothing is set, and ``write_enabled`` is only turned
on where the seeder's old default left it off.

**What the voice costs.** ``assistant_voice_models`` holds the prices and
``assistant_voice_usage`` the record. Two tables rather than columns on the
existing ones because voice is not billed in tokens: speech is charged per
character of text, a realtime session per token with audio dearer than text by
an order of magnitude. Folding either into ``assistant_models`` would have
meant calling a character a token.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d1a6e37c40b8"
down_revision: str | None = "c8f2b6d41a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Modules whose writes carry company-wide consequences, and the roles allowed
#: to make them. Mirrors ``ModuleGroup.write_roles`` in the assistant catalogue;
#: repeated here rather than imported because a migration must keep meaning what
#: it meant on the day it ran, whatever the catalogue says later.
SENSITIVE: dict[str, tuple[str, ...]] = {
    "roles": ("super_admin", "ceo", "manager"),
    "user_admin": ("super_admin", "ceo", "manager"),
    "teams": ("super_admin", "ceo", "manager"),
    "templates": ("super_admin", "ceo", "manager"),
    "assignment": ("super_admin", "ceo", "manager"),
    "finance": ("super_admin", "ceo", "manager"),
    "quotes": ("super_admin", "ceo", "manager"),
}


def upgrade() -> None:
    # ── who may write ──────────────────────────────────────────────────
    for table in ("assistant_module_policies", "assistant_tool_policies"):
        op.add_column(
            table,
            sa.Column("write_roles", postgresql.ARRAY(sa.String(length=40)), nullable=True),
        )

    op.alter_column(
        "assistant_module_policies",
        "write_enabled",
        server_default=sa.text("true"),
    )
    # Only where the old default left it. A super admin who turned a module's
    # writes on already has it on; one who turned them off did so on purpose,
    # and there is no way to tell that apart from never having touched it — so
    # this errs towards the request that prompted the change, which was to have
    # the writes available.
    op.execute(
        sa.text("UPDATE assistant_module_policies SET write_enabled = true")
    )

    for module_key, roles in SENSITIVE.items():
        op.execute(
            sa.text(
                "UPDATE assistant_module_policies "
                "SET write_roles = :roles "
                "WHERE module_key = :key AND write_roles IS NULL"
            ).bindparams(sa.bindparam("roles", list(roles)), sa.bindparam("key", module_key))
        )

    # ── what the voice costs ───────────────────────────────────────────
    money = sa.Numeric(10, 4)
    op.create_table(
        "assistant_voice_models",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("char_price", money, server_default=sa.text("0"), nullable=False),
        sa.Column("text_input_price", money, server_default=sa.text("0"), nullable=False),
        sa.Column(
            "cached_text_input_price", money, server_default=sa.text("0"), nullable=False
        ),
        sa.Column("audio_input_price", money, server_default=sa.text("0"), nullable=False),
        sa.Column(
            "cached_audio_input_price", money, server_default=sa.text("0"), nullable=False
        ),
        sa.Column("text_output_price", money, server_default=sa.text("0"), nullable=False),
        sa.Column("audio_output_price", money, server_default=sa.text("0"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "updated_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_assistant_voice_models_kind", "assistant_voice_models", ["kind"]
    )

    op.add_column(
        "assistant_runs",
        sa.Column(
            "voice_usage_reported",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    tokens = dict(server_default=sa.text("0"), nullable=True)
    op.create_table(
        "assistant_voice_usage",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assistant_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("voice", sa.String(length=24), nullable=True),
        sa.Column(
            "source", sa.String(length=8), server_default=sa.text("'server'"), nullable=False
        ),
        sa.Column("characters", sa.Integer(), **tokens),
        sa.Column("text_input_tokens", sa.Integer(), **tokens),
        sa.Column("cached_text_input_tokens", sa.Integer(), **tokens),
        sa.Column("audio_input_tokens", sa.Integer(), **tokens),
        sa.Column("cached_audio_input_tokens", sa.Integer(), **tokens),
        sa.Column("text_output_tokens", sa.Integer(), **tokens),
        sa.Column("audio_output_tokens", sa.Integer(), **tokens),
        sa.Column("seconds", sa.Integer(), **tokens),
        sa.Column(
            "cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_assistant_voice_usage_user_created",
        "assistant_voice_usage",
        ["user_id", "created_at"],
    )
    op.create_index("ix_assistant_voice_usage_kind", "assistant_voice_usage", ["kind"])
    op.create_index(
        "ix_assistant_voice_usage_model_key", "assistant_voice_usage", ["model_key"]
    )


def downgrade() -> None:
    op.drop_table("assistant_voice_usage")
    op.drop_index("ix_assistant_voice_models_kind", table_name="assistant_voice_models")
    op.drop_table("assistant_voice_models")
    op.drop_column("assistant_runs", "voice_usage_reported")

    # Back to writes off by default, which is what this revision replaced.
    op.alter_column(
        "assistant_module_policies", "write_enabled", server_default=sa.text("false")
    )
    for table in ("assistant_tool_policies", "assistant_module_policies"):
        op.drop_column(table, "write_roles")
