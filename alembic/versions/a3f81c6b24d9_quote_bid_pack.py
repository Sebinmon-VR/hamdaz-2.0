"""quote bid pack: tender particulars, landed cost, compliance, portal fields

A quote request has until now been a Zoho estimate: a customer, some dates, some
terms and a list of priced lines. That is all a quote needs when somebody rings
up and asks for a price. A *tender* is not that — it arrives as an RFP with
numbered clauses, a mandatory specification, a portal with named cells, and a
price that has to be justified line by line because the buyer will be shown the
principal's own quotation next to ours.

This adds the bid layer on top, in four parts:

* bid particulars on ``quote_requests`` — the event number, the buying entity,
  the manufacturer the RFP specifies, the Incoterm it demands, the origin, the
  validity, and the landed-cost *inputs* (FX rate, duty, financing, markup).
* ``quote_cost_lines`` — the build-up on top of the goods: haulage, freight,
  certificates, clearance, the bank.
* ``quote_compliance_items`` — one RFP requirement per row against what the
  supplier actually offered, with an owner for the gap.
* ``quote_submission_fields`` — the values to be typed into the buyer's portal,
  and where each one goes.

**No totals are stored.** The CIF value, the duty, the landed cost, the margin
ladder and the uplift the buyer will see are all computed on read from the
columns here — see ``app/quoting/bidpack.py``. A stored total and the inputs it
came from disagree the first time anybody edits one, and the one people believe
is always the wrong one.

Every added column is nullable or carries a server default, so existing quotes —
which are all estimates rather than bids — are untouched and stay valid.

Revision ID: a3f81c6b24d9
Revises: b7e2d41c9a05
Create Date: 2026-09-16 11:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a3f81c6b24d9'
down_revision: str | None = 'b7e2d41c9a05'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The bid particulars, as (name, type). All nullable: a quote raised for
#: somebody who asked for a price over the phone fills in none of them.
_NULLABLE_COLUMNS: list[tuple[str, sa.types.TypeEngine]] = [
    ("rfp_number", sa.String(length=120)),
    ("buying_entity", sa.String(length=200)),
    ("line_item_ref", sa.String(length=120)),
    ("manufacturer_name", sa.String(length=200)),
    ("manufacturer_part_number", sa.String(length=120)),
    ("manufacturer_class_no", sa.String(length=120)),
    ("incoterm_required", sa.String(length=40)),
    ("incoterm_place", sa.String(length=200)),
    ("ship_to", sa.String(length=200)),
    ("requested_delivery_date", sa.DateTime(timezone=True)),
    ("delivery_days", sa.Integer()),
    ("country_of_origin", sa.String(length=2)),
    ("mode_of_shipment", sa.String(length=60)),
    ("bid_validity_days", sa.Integer()),
    ("bid_reference", sa.String(length=120)),
    ("technical_verdict", sa.Text()),
    ("commercial_verdict", sa.Text()),
    ("supplier_currency", sa.String(length=3)),
    ("fx_rate", sa.Numeric(18, 8)),
    ("target_markup_percent", sa.Numeric(7, 3)),
    ("submission_unit_price", sa.Numeric(18, 4)),
    ("submission_total", sa.Numeric(18, 2)),
]

#: Rates that mean zero when nobody has said otherwise, rather than "unknown".
#: A duty rate of nothing is a real answer — plenty of goods carry none.
_DEFAULTED_COLUMNS: list[tuple[str, sa.types.TypeEngine, str]] = [
    ("customs_duty_percent", sa.Numeric(6, 3), "0"),
    ("financing_rate_percent", sa.Numeric(6, 3), "0"),
    ("cash_exposure_days", sa.Integer(), "0"),
]


def upgrade() -> None:
    for name, type_ in _NULLABLE_COLUMNS:
        op.add_column("quote_requests", sa.Column(name, type_, nullable=True))

    for name, type_, default in _DEFAULTED_COLUMNS:
        op.add_column(
            "quote_requests",
            sa.Column(name, type_, nullable=False, server_default=sa.text(default)),
        )

    op.add_column(
        "quote_requests",
        sa.Column(
            "discloses_principal_price",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # The event number is what a bid is looked up by — in a clarification, on a
    # PO, in a conversation with the buyer. Indexed for that reason and no other.
    op.create_index(
        "ix_quote_requests_rfp_number", "quote_requests", ["rfp_number"], unique=False
    )

    # ── the landed-cost build-up ───────────────────────────────────────
    # The goods are not a row here. They come from the quote's own priced lines,
    # so that repricing from another supplier carries the cost with it rather
    # than leaving the old one sitting at the top of the build-up.
    op.create_table(
        "quote_cost_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("label", sa.String(length=300), nullable=False),
        sa.Column("basis", sa.String(length=300), nullable=True),
        sa.Column("amount_source", sa.Numeric(18, 4), nullable=True),
        sa.Column("source_currency", sa.String(length=3), nullable=True),
        sa.Column(
            "amount_base", sa.Numeric(18, 4), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "is_principal", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("is_firm", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["request_id"], ["quote_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quote_cost_lines_request", "quote_cost_lines", ["request_id", "position"]
    )

    # ── the compliance matrix ──────────────────────────────────────────
    op.create_table(
        "quote_compliance_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ref", sa.String(length=20), nullable=True),
        sa.Column("area", sa.String(length=20), nullable=False),
        sa.Column("requirement", sa.Text(), nullable=False),
        sa.Column("source_clause", sa.String(length=120), nullable=True),
        sa.Column("supplier_position", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        # Null unless the row belongs on the red-flag list.
        sa.Column("severity", sa.String(length=12), nullable=True),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("owner", sa.String(length=200), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["request_id"], ["quote_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quote_compliance_request", "quote_compliance_items", ["request_id", "position"]
    )
    op.create_index(
        "ix_quote_compliance_severity", "quote_compliance_items", ["request_id", "severity"]
    )

    # ── the portal checklist ───────────────────────────────────────────
    op.create_table(
        "quote_submission_fields",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("clause", sa.String(length=40), nullable=True),
        sa.Column("label", sa.String(length=300), nullable=False),
        sa.Column("destination", sa.String(length=120), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "is_mandatory", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["request_id"], ["quote_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quote_submission_request", "quote_submission_fields", ["request_id", "position"]
    )


def downgrade() -> None:
    op.drop_index("ix_quote_submission_request", table_name="quote_submission_fields")
    op.drop_table("quote_submission_fields")

    op.drop_index("ix_quote_compliance_severity", table_name="quote_compliance_items")
    op.drop_index("ix_quote_compliance_request", table_name="quote_compliance_items")
    op.drop_table("quote_compliance_items")

    op.drop_index("ix_quote_cost_lines_request", table_name="quote_cost_lines")
    op.drop_table("quote_cost_lines")

    op.drop_index("ix_quote_requests_rfp_number", table_name="quote_requests")
    op.drop_column("quote_requests", "discloses_principal_price")
    for name, _, _ in reversed(_DEFAULTED_COLUMNS):
        op.drop_column("quote_requests", name)
    for name, _ in reversed(_NULLABLE_COLUMNS):
        op.drop_column("quote_requests", name)
