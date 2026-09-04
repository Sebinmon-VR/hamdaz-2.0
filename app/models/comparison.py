"""Supplier quote comparisons.

A presales engineer collects quotes from several suppliers for the same
requirement and has to decide who to buy from. This is the record of that: the
suppliers, what each quoted line by line, and the comparison that came out of it.

Three tables rather than one JSON blob, because the line items are the point.
"Which supplier is cheapest on the drier unit" and "what would a split award
save" are questions about rows, and answering them by unpacking a document on
every read would make the interesting queries the awkward ones.

Money is ``Numeric``, never float. These are bid prices that get totalled and
compared; binary floating point turns 0.1 + 0.2 into a discrepancy someone has
to explain to a supplier.

Nothing here is written by the model directly. Claude reads the documents and
proposes structure; a person can correct it before it is saved, and the
arithmetic is always done in Python. ``extraction_note`` records where a value
came from so a surprising number can be traced back.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User


class ComparisonStatus(StrEnum):
    #: Being worked on. The only state that may be edited.
    DRAFT = "draft"
    #: The engineer is happy with it and has saved it as the record.
    SAVED = "saved"


class QuoteSource(StrEnum):
    """Where a supplier's numbers came from — worth keeping.

    An extracted quote may carry a model's misreading; a typed one carries a
    person's. They are different failure modes and reviewers treat them
    differently.
    """

    UPLOAD = "upload"
    MANUAL = "manual"


class QuoteComparison(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "quote_comparisons"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)

    status: Mapped[ComparisonStatus] = mapped_column(
        String(20), default=ComparisonStatus.DRAFT, nullable=False, index=True
    )

    #: Everything is priced in this currency for comparison. Quotes that arrive
    #: in another are converted on the way in, and the rate used is recorded on
    #: the quote so the comparison stays reproducible.
    currency: Mapped[str] = mapped_column(String(3), default="AED", nullable=False)

    created_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: The finished analysis: matched line groups, per-supplier totals, the
    #: split-award result and the insights. Stored rather than recomputed so a
    #: saved comparison still reads the same after prices change, and so the
    #: record shows what was actually decided on.
    analysis: Mapped[dict | None] = mapped_column(JSONB)
    analysed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id], lazy="joined")
    quotes: Mapped[list[SupplierQuote]] = relationship(
        back_populates="comparison",
        cascade="all, delete-orphan",
        order_by="SupplierQuote.created_at",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<QuoteComparison {self.title!r} {self.status}>"


class SupplierQuote(Base, UUIDPrimaryKey, Timestamped):
    """One supplier's offer within a comparison."""

    __tablename__ = "supplier_quotes"
    __table_args__ = (
        Index("ix_supplier_quotes_comparison", "comparison_id"),
    )

    comparison_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quote_comparisons.id", ondelete="CASCADE"),
        nullable=False,
    )

    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False)
    quote_number: Mapped[str | None] = mapped_column(String(100))
    quote_date: Mapped[str | None] = mapped_column(String(40))
    # Free text, not codes. A real quote says "2 weeks, ex stock, subject to
    # export clearance" where a form would have offered a dropdown, and the
    # caveat is often the deciding part. Length-capped, that sentence either
    # loses its condition or fails the upload, so these are stored whole.
    #: How long the price holds. Frequently the deciding factor when two bids
    #: are close, and frequently the thing nobody reads.
    validity: Mapped[str | None] = mapped_column(Text)
    delivery_time: Mapped[str | None] = mapped_column(Text)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    warranty: Mapped[str | None] = mapped_column(Text)
    incoterms: Mapped[str | None] = mapped_column(String(60))
    contact: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    #: As quoted, before conversion.
    currency: Mapped[str] = mapped_column(String(3), default="AED", nullable=False)
    #: 1 unit of ``currency`` in the comparison's currency. 1 when they match.
    #: Recorded so a converted total can be checked years later.
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(1), nullable=False)

    #: What the supplier wrote as the total, when they wrote one. Kept apart
    #: from the sum of the lines: when the two disagree it usually means a
    #: discount or a fee that is not on any line, and that is worth surfacing
    #: rather than silently overwriting.
    quoted_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    discount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    freight: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    tax: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))

    source: Mapped[QuoteSource] = mapped_column(
        String(20), default=QuoteSource.MANUAL, nullable=False
    )
    #: The uploaded document, kept so a disputed figure can be checked against
    #: what the supplier actually sent.
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_type: Mapped[str | None] = mapped_column(String(100))
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)

    #: Anything the model was unsure of — an illegible figure, a line with no
    #: price. Surfaced next to the quote so a reviewer looks there first.
    extraction_note: Mapped[str | None] = mapped_column(Text)

    comparison: Mapped[QuoteComparison] = relationship(back_populates="quotes")
    items: Mapped[list[SupplierQuoteItem]] = relationship(
        back_populates="quote",
        cascade="all, delete-orphan",
        order_by="SupplierQuoteItem.position",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<SupplierQuote {self.supplier_name!r} {self.currency}>"


class SupplierQuoteItem(Base, UUIDPrimaryKey, Timestamped):
    """One priced line on one supplier's quote."""

    __tablename__ = "supplier_quote_items"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_quote_item_quantity_positive"),
        Index("ix_supplier_quote_items_quote", "quote_id"),
    )

    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supplier_quotes.id", ondelete="CASCADE"), nullable=False
    )

    #: Order on the original document, so a quote reads as the supplier wrote it.
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    description: Mapped[str] = mapped_column(Text, nullable=False)
    part_number: Mapped[str | None] = mapped_column(String(120))
    brand: Mapped[str | None] = mapped_column(String(120))
    unit: Mapped[str | None] = mapped_column(String(40))

    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(1), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(0), nullable=False)
    #: As printed on the quote. Kept even when it disagrees with quantity ×
    #: unit price — the disagreement is a finding, not a rounding error to hide.
    line_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    #: Free text for the same reason as the quote's own delivery time.
    lead_time: Mapped[str | None] = mapped_column(Text)

    quote: Mapped[SupplierQuote] = relationship(back_populates="items")

    @property
    def computed_total(self) -> Decimal:
        return (self.quantity or Decimal(0)) * (self.unit_price or Decimal(0))

    def __repr__(self) -> str:
        return f"<SupplierQuoteItem {self.description[:30]!r} x{self.quantity}>"
