"""Enquiry analysis: what a pre-sales task's documents ask for, and what we know of it.

Three tables:

* ``enquiry_analyses`` — one per Proposals task. The latest run's outcome: the
  summary, the conditions that are not line items, what the documents leave
  unsaid, what the run cost, and where its reports were filed.
* ``enquiry_documents`` — every document the analysis has seen for the task:
  the task's list attachments, the files in its folder in the Proposal Team
  Channel library, and what people uploaded on the page (filed into that
  folder). No bytes are kept here; the library is the store.
* ``enquiry_lines`` — one requirement line, with what the history says of it
  (where we met it before, from whom, at what rate) and, for a new one, what
  the web says (manufacturer, likely suppliers, a rough price).

A supplier quotation found among the documents is read by the comparison
module's reader and stored as a ``supplier_quotes`` row like any other, under a
comparison the analysis owns — so the supplier library can be built from it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPrimaryKey
from app.models.user import User


class EnquiryAnalysis(Base, UUIDPrimaryKey, Timestamped):
    """One task's analysis. A re-run replaces its lines and keeps its documents."""

    __tablename__ = "enquiry_analyses"

    #: The Proposals list item id. One analysis per task.
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    task_title: Mapped[str] = mapped_column(Text, nullable=False)
    end_user: Mapped[str | None] = mapped_column(String(300))
    bid_closing_date: Mapped[date | None] = mapped_column(Date)

    #: idle, running, done, failed.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="idle", server_default="idle"
    )
    #: What a running analysis is doing now, or why it failed.
    stage: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    summary: Mapped[str | None] = mapped_column(Text)
    customer: Mapped[str | None] = mapped_column(String(300))
    deadline: Mapped[str | None] = mapped_column(String(40))
    #: Conditions that are not line items: certificates, delivery, warranty.
    conditions: Mapped[list | None] = mapped_column(JSONB)
    #: What the documents do not say that a supplier would need to know.
    missing: Mapped[list | None] = mapped_column(JSONB)
    #: What the last run could not do, in words: a document too large, Zoho
    #: refusing, the web lookup failing. Shown with the result, never hidden.
    run_notes: Mapped[list | None] = mapped_column(JSONB)
    #: The last run, step by step: ``[{at, level, message}]``, oldest first.
    #: Written as the run goes, so the page can show it live.
    run_log: Mapped[list | None] = mapped_column(JSONB)

    web_search: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    model: Mapped[str | None] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal(0), server_default=text("0")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: The task's folder under Proposal Team Channel, as found by its title.
    drive_folder: Mapped[str | None] = mapped_column(Text)
    drive_folder_url: Mapped[str | None] = mapped_column(Text)
    report_pdf_url: Mapped[str | None] = mapped_column(Text)
    report_xlsx_url: Mapped[str | None] = mapped_column(Text)
    filing_error: Mapped[str | None] = mapped_column(Text)

    #: Where the supplier quotes found among the documents are kept.
    comparison_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote_comparisons.id", ondelete="SET NULL")
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    run_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    run_by: Mapped[User | None] = relationship(foreign_keys=[run_by_id], lazy="joined")
    documents: Mapped[list[EnquiryDocument]] = relationship(
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="EnquiryDocument.created_at",
        lazy="selectin",
    )
    lines: Mapped[list[EnquiryLine]] = relationship(
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="EnquiryLine.position",
        lazy="selectin",
    )


class EnquiryDocument(Base, UUIDPrimaryKey, Timestamped):
    """One document of the task, wherever it came from."""

    __tablename__ = "enquiry_documents"
    __table_args__ = (UniqueConstraint("analysis_id", "origin_key"),)

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("enquiry_analyses.id", ondelete="CASCADE"), nullable=False
    )
    #: attachment (on the list item), folder (in the task folder), upload
    #: (put there from the page — also in the task folder).
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    #: ``attachment:<name>`` or ``drive:<item id>`` — how a re-run knows it.
    origin_key: Mapped[str] = mapped_column(String(400), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Where in the task folder, for a file found there.
    path: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(Integer)
    drive_item_id: Mapped[str | None] = mapped_column(String(120))
    web_url: Mapped[str | None] = mapped_column(Text)
    #: requirement, supplier_quote, other — what the run took it for.
    kind: Mapped[str | None] = mapped_column(String(30))
    #: pending, read, skipped, failed.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    note: Mapped[str | None] = mapped_column(Text)
    supplier_quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supplier_quotes.id", ondelete="SET NULL")
    )
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    analysis: Mapped[EnquiryAnalysis] = relationship(back_populates="documents")


class EnquiryLine(Base, UUIDPrimaryKey, Timestamped):
    """One thing the customer asks for, and what we know of it."""

    __tablename__ = "enquiry_lines"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("enquiry_analyses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    part_number: Mapped[str | None] = mapped_column(String(120))
    brand: Mapped[str | None] = mapped_column(String(120))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    unit: Mapped[str | None] = mapped_column(String(40))
    specification: Mapped[str | None] = mapped_column(Text)
    source_document: Mapped[str | None] = mapped_column(String(255))

    #: recent (met within the recent window), history (met before that), new.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    #: Where we met it: ``[{source, ref, date, counterparty, rate, currency,
    #: quantity, description, part_number, score, url}]``, best first.
    history: Mapped[list | None] = mapped_column(JSONB)
    #: Who might supply it: ``[{name, source, role, website, email, phone,
    #: country, evidence, last_rate, currency, last_date, partner}]``.
    suppliers: Mapped[list | None] = mapped_column(JSONB)
    #: What the web said, for a new item: manufacturer, price range, sources.
    web: Mapped[dict | None] = mapped_column(JSONB)

    analysis: Mapped[EnquiryAnalysis] = relationship(back_populates="lines")
