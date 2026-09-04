"""Request and response shapes for quote comparison.

One shape does double duty: what extraction returns is exactly what the save
endpoint accepts. That is deliberate — the engineer uploads documents, gets a
draft back, corrects whatever the model misread, and posts the corrected version.
A separate "extracted" type would make that round trip a translation exercise and
would quietly discourage the correction step, which is the one that matters.

Money crosses the wire as ``Decimal``. Pydantic serialises it as a JSON number
without going through binary floating point, so a price is the same on both
sides of the request.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.comparison import ComparisonStatus, QuoteSource


class ItemIn(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    part_number: str | None = Field(default=None, max_length=120)
    brand: str | None = Field(default=None, max_length=120)
    unit: str | None = Field(default=None, max_length=40)
    quantity: Decimal = Field(default=Decimal(1), ge=0)
    unit_price: Decimal = Field(default=Decimal(0), ge=0)
    #: As printed on the quote. Left null when the supplier showed none — it is
    #: then computed as quantity x unit price rather than invented.
    line_total: Decimal | None = None
    #: Free text as the supplier wrote it, so it carries its conditions.
    lead_time: str | None = Field(default=None, max_length=2000)


class QuoteIn(BaseModel):
    supplier_name: str = Field(min_length=1, max_length=200)
    quote_number: str | None = Field(default=None, max_length=100)
    quote_date: str | None = Field(default=None, max_length=40)
    currency: str = Field(default="AED", min_length=3, max_length=3)
    #: One unit of this quote's currency in the comparison's currency. Left at 1
    #: when they are the same. No rate is fetched anywhere — an automatic rate
    #: would silently change a saved comparison's numbers over time.
    fx_rate: Decimal = Field(default=Decimal(1), gt=0)

    # Free text, not codes — see the note on the columns behind them. The cap
    # is a sanity limit on a payload, not a shape the terms have to fit.
    validity: str | None = Field(default=None, max_length=2000)
    delivery_time: str | None = Field(default=None, max_length=2000)
    payment_terms: str | None = Field(default=None, max_length=2000)
    warranty: str | None = Field(default=None, max_length=2000)
    incoterms: str | None = Field(default=None, max_length=60)
    contact: str | None = Field(default=None, max_length=200)
    notes: str | None = None

    discount: Decimal | None = None
    freight: Decimal | None = None
    tax: Decimal | None = None
    quoted_total: Decimal | None = None

    items: list[ItemIn] = Field(default_factory=list)

    source: QuoteSource = QuoteSource.MANUAL
    file_name: str | None = Field(default=None, max_length=255)
    #: What the model was unsure about. Carried through the round trip so it is
    #: still attached to the quote when a reviewer reads the saved comparison.
    extraction_note: str | None = None


class ComparisonIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = None
    #: Everything is compared in this currency.
    currency: str = Field(default="AED", min_length=3, max_length=3)
    quotes: list[QuoteIn] = Field(default_factory=list)


class AnalyseIn(BaseModel):
    """Compare without saving, and without needing an upload.

    This is the manual path's endpoint: type the suppliers into a form, post
    them, see the comparison. Nothing is written to the database.
    """

    currency: str = Field(default="AED", min_length=3, max_length=3)
    quotes: list[QuoteIn] = Field(min_length=1)


class ExtractionFailure(BaseModel):
    file_name: str
    error: str


class ExtractionOut(BaseModel):
    """Draft quotes read from the uploaded documents.

    Nothing here is saved. The caller reviews and corrects these, then posts
    them to ``/analyse`` or ``/comparisons``.
    """

    quotes: list[QuoteIn]
    #: One entry per document that could not be read. The rest still come back —
    #: one bad scan should not lose three good quotes.
    failed: list[ExtractionFailure] = Field(default_factory=list)
    #: What the extraction cost, so the running spend is visible rather than a
    #: line on next month's bill.
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ItemOut(ItemIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int


class QuoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    supplier_name: str
    quote_number: str | None
    quote_date: str | None
    currency: str
    fx_rate: Decimal
    validity: str | None
    delivery_time: str | None
    payment_terms: str | None
    warranty: str | None
    incoterms: str | None
    contact: str | None
    notes: str | None
    discount: Decimal | None
    freight: Decimal | None
    tax: Decimal | None
    quoted_total: Decimal | None
    source: str
    file_name: str | None
    file_type: str | None
    extraction_note: str | None
    items: list[ItemOut]
    #: Present only when the original document was kept.
    document_url: str | None = None


class ComparisonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    reference: str | None
    notes: str | None
    currency: str
    status: ComparisonStatus
    created_by_id: uuid.UUID
    #: Filled in by the router; not a column on the model.
    created_by_name: str | None = None
    created_at: datetime
    updated_at: datetime
    analysed_at: datetime | None
    #: The stored comparison — matched groups, totals, split award, insights.
    analysis: dict[str, Any] | None
    quotes: list[QuoteOut]


class ComparisonSummaryOut(BaseModel):
    """A list row. Deliberately does not carry the analysis or the line items —
    an index of fifty comparisons should not ship fifty analyses."""

    id: uuid.UUID
    title: str
    reference: str | None
    currency: str
    status: ComparisonStatus
    created_by_name: str | None
    created_at: datetime
    supplier_count: int
    item_count: int
    #: Headline figure, so the list is scannable without opening each one.
    best_total: Decimal | None
    best_supplier: str | None


class AnalysisOut(BaseModel):
    """The comparison itself, unsaved."""

    currency: str
    analysis: dict[str, Any]
