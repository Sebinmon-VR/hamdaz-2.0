"""Reading a supplier quote, with no model behind it.

One reader: the local parser in ``parsing.py``. Most supplier quotes are
machine generated and entirely regular — a table with a description column and
a price column, and labelled fields around it. Reading one of those is
deterministic work: it costs nothing, gives the same answer every time, and
needs no network, no key and no credit.

There used to be a second reader, a model, for the documents the parser could
not handle. It is gone — the credit behind it ran out, and a reader that stops
working when a bill is unpaid is not a reader a business process can stand on.
What it was for is covered two other ways:

* **Layouts with no ruled table** are read by rebuilding the table from where
  the words sit on the page (``documents.table_from_words``), and by a
  line-shape reader in ``parsing.py`` for text that is a table only in spirit.
* **Scans and photographs** go through an OCR engine when one is installed
  (``documents._ocr``). When none is, the document is declined with a message
  that says so, and the supplier's lines are typed in on the screen instead —
  a path that exists in its own right, not as a fallback.

Either way nothing here does arithmetic. The parser transcribes; the totals are
computed in Python from what it read, because a figure a reader added up is a
figure nobody can check.

**Honesty over completeness.** The parser leaves a field blank rather than
guess, and says so in ``note``. A missing delivery time is a fact a reviewer
can act on; an invented one is a fact they cannot.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field, ValidationError

from app.comparison.documents import Readable, ocr_available
from app.core.config import Settings
from app.core.llm import TextModel

logger = logging.getLogger("hamdaz.comparison")


# Every field is required and plainly typed, with "" and 0 meaning "not on the
# document". The shape predates the model's departure — it was the shape a
# constrained decoder could handle — and it is kept because the router, the
# tests and the comparison service all read it, and because a blank that means
# "not there" is a useful convention on its own. :func:`blank_to_none` turns
# the blanks back into nulls on the way into the database.


class ExtractedItem(BaseModel):
    """One priced line from a quotation."""

    description: str = Field(description="The item as written on the quote")
    part_number: str = Field(description="Supplier or manufacturer part number")
    brand: str = Field(description="Make or manufacturer, if named")
    unit: str = Field(description="each, set, metre, box...")
    quantity: float = Field(description="As printed. 1 if the quote shows no quantity")
    unit_price: float = Field(description="Price for ONE unit, as printed")
    line_total: float = Field(description="The line total as PRINTED. 0 if the quote shows none")
    lead_time: str = Field(description="Lead time for this line, if per-line")


class ExtractedQuote(BaseModel):
    """One supplier's quotation."""

    supplier_name: str = Field(description="The company that issued the quote")
    quote_number: str = Field(description="Quote or offer number")
    quote_date: str = Field(description="As printed, any format")
    currency: str = Field(description="ISO code such as AED, USD, EUR")
    validity: str = Field(description="How long the price holds")
    delivery_time: str = Field(description="Delivery or lead time")
    payment_terms: str = Field(description="Payment terms")
    warranty: str = Field(description="Warranty or guarantee")
    incoterms: str = Field(description="EXW, FOB, CIF, DDP...")
    contact: str = Field(description="Name or email of the sender")

    discount: float = Field(description="Total discount, if shown separately")
    freight: float = Field(description="Shipping or handling, if shown")
    tax: float = Field(description="VAT or tax, if shown")
    quoted_total: float = Field(description="The grand total as PRINTED")

    items: list[ExtractedItem] = Field(description="Every priced line on the quote")
    note: str = Field(description="Anything uncertain or unreadable, naming the line")


#: The shape asked of a model, in words, kept beside the models so the two
#: cannot drift apart.
JSON_SHAPE: dict = {
    "supplier_name": "string",
    "quote_number": "string",
    "quote_date": "string",
    "currency": "ISO code, e.g. AED",
    "validity": "string",
    "delivery_time": "string",
    "payment_terms": "string",
    "warranty": "string",
    "incoterms": "string",
    "contact": "string",
    "discount": 0,
    "freight": 0,
    "tax": 0,
    "quoted_total": 0,
    "items": [
        {
            "description": "string",
            "part_number": "string",
            "brand": "string",
            "unit": "string",
            "quantity": 0,
            "unit_price": 0,
            "line_total": 0,
            "lead_time": "string",
        }
    ],
    "note": "anything uncertain, naming the line",
}


def blank_to_none(value):
    """``""`` and ``0`` mean "not on the document", not "zero".

    The extracted shape uses blanks so every field can be required; this
    restores the distinction that matters downstream, where a missing delivery
    time and an empty one read very differently.
    """
    if value == "" or value == 0:
        return None
    return value


class ExtractionError(Exception):
    """A document could not be read. Safe to show a user."""


class QuoteExtractor:
    """Reads supplier quote documents. Holds no client and needs no key.

    Kept as a class, constructed with the settings, because the application
    wires one onto ``app.state`` and every route that reads documents asks for
    it there. The settings are not read: nothing here is configurable any more.
    """

    def __init__(self, settings: Settings, model: TextModel | None = None) -> None:
        self._settings = settings
        self._model = model

    @property
    def configured(self) -> bool:
        """Whether a model stands behind the parser for the documents it
        declines. The comparison's matching never uses one either way."""
        return self._model is not None and self._model.configured

    async def read(self, readable: Readable) -> ExtractedQuote:
        """One document in, one validated quote out — or an error that says
        what to do instead."""
        result = await self._read(readable)
        if isinstance(result, ExtractionError):
            raise result
        return result

    async def _read(self, readable: Readable) -> ExtractedQuote | ExtractionError:
        if readable.kind != "text":
            return ExtractionError(cannot_read_scan(readable.file_name))
        if (parsed := parse_locally(readable)) is not None:
            return parsed
        if (modelled := await self._read_with_model(readable)) is not None:
            return modelled
        return ExtractionError(
            f"No price table could be found in {readable.file_name!r}. It may be a "
            f"covering letter rather than a quotation, or laid out in a way this "
            f"cannot follow. Type the supplier's lines in instead."
        )

    async def _read_with_model(self, readable: Readable) -> ExtractedQuote | None:
        """The second pass: the text the parser could not follow, read by a
        model into the same shape and validated the same way. Its note says
        which model, so a reviewer checks those figures first."""
        if not self.configured or not readable.text:
            return None
        answer = await self._model.extract(
            instructions=(
                f"Transcribe this supplier quotation ({readable.file_name}). One entry "
                f"in `items` per priced line; do not include subtotal, VAT or total rows "
                f"as items — those go in the header fields. Copy prices digit for digit. "
                f"`quantity` is the quantity as printed, and 1 when the quote prints none "
                f"beside the price. `unit_price` is the price for ONE unit. `currency` is "
                f"the ISO code; infer it from a symbol when it is not spelled out "
                f"($ is USD, Dhs or AED is AED, € is EUR, £ is GBP)."
            ),
            text=readable.text,
            shape=JSON_SHAPE,
            max_tokens=6000,
        )
        if answer is None:
            return None
        payload, who = answer
        try:
            quote = ExtractedQuote.model_validate(payload)
        except ValidationError as exc:
            logger.info("model answer for %s did not validate: %s", readable.file_name, exc)
            return None
        if not quote.items:
            return None
        settle_model_reading(quote, readable.text)
        quote.note = (
            f"Read by a model ({who}) because no price table could be followed. "
            f"Check every figure against the document. "
            + (quote.note or "")
        ).strip()
        return quote

    async def read_all(
        self, readables: list[Readable]
    ) -> list[ExtractedQuote | ExtractionError]:
        """Every document, each failure kept with its own file.

        Returned rather than raised: one unreadable scan among four suppliers
        should leave the other three usable, not lose the whole upload.
        """
        return [await self._read(r) for r in readables]


def settle_model_reading(quote: ExtractedQuote, text: str | None) -> list[str]:
    """What a model is not trusted to decide, decided here.

    A quantity of 0 is a line nobody is buying, and no supplier prints one;
    it means the quote showed no quantity, and that is one unit. A unit price
    of 0 beside a line total is the total divided out. A blank currency is
    read off the document's own symbols and codes, the way the parser reads
    them — the model has been known to infer "$ means USD" in its note and
    leave the field empty. Returns what was settled, for the note.
    """
    from app.comparison.parsing import _currency

    settled: list[str] = []
    for item in quote.items:
        if (item.quantity or 0) <= 0:
            item.quantity = 1
            settled.append("quantity")
        if (item.unit_price or 0) <= 0 and (item.line_total or 0) > 0 and item.quantity > 0:
            item.unit_price = round(item.line_total / item.quantity, 4)
            settled.append("unit price")
        elif (item.line_total or 0) <= 0 and (item.unit_price or 0) > 0:
            item.line_total = round(item.unit_price * item.quantity, 2)
    if not (quote.currency or "").strip():
        found = _currency(text or "")
        if found:
            quote.currency = found
            settled.append("currency")
    if "quantity" in settled:
        quote.note = (
            (quote.note or "")
            + " No quantities were printed beside the prices; each such line is one unit."
        ).strip()
    if "currency" in settled:
        quote.note = (
            (quote.note or "") + f" Currency read from the document's symbols as {quote.currency}."
        ).strip()
    return settled


def cannot_read_scan(file_name: str) -> str:
    """Why a scan or a photograph was declined, and what to do about it."""
    if ocr_available():
        return (
            f"{file_name!r} is a scan or a photograph and the text recognition could "
            f"not make out a price table in it. Type the supplier's lines in instead."
        )
    return (
        f"{file_name!r} is a scan or a photograph with no text to read. Nothing "
        f"here can read an image without an OCR engine installed, so type the "
        f"supplier's lines in instead — or ask the supplier for the PDF their "
        f"system produced."
    )


def to_decimal(value: float | str | None, default: Decimal | None = None) -> Decimal | None:
    """A parsed number as exact currency.

    Goes through ``str`` deliberately: ``Decimal(1234.56)`` inherits the float's
    error, ``Decimal("1234.56")`` does not. Money is compared and totalled here,
    so the difference eventually shows up in front of a supplier.
    """
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def parse_locally(readable: Readable) -> ExtractedQuote | None:
    """The parser, imported late.

    ``parsing`` needs ``ExtractedQuote`` from this module, so importing it at the
    top would be circular. The call is cheap and happens once per document.
    """
    from app.comparison.parsing import parse

    return parse(readable)
