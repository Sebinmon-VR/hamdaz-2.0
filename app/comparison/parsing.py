"""Reading a supplier quote without a model.

This runs first, and Claude only sees a document this could not handle. Most
supplier quotes are machine-generated and thoroughly regular: a table with a
description column and a price column, and a handful of labelled fields around
it. Parsing that is deterministic work, and deterministic work should not cost
money or vary between runs.

The design rests on one rule: **admit failure loudly**. A parser that half-works
is worse than one that declines, because a quote with three of its eight lines
found looks exactly like a quote with three lines. So ``parse`` returns ``None``
whenever it is not confident, and the caller pays for the model instead. Every
threshold here is set to fail towards the model rather than towards a plausible
wrong answer.

What it does *not* attempt: deciding whether two suppliers' items are the same
thing. That is judgement, it is where the money is, and it stays with the model.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Final

from app.comparison.documents import Readable
from app.comparison.extraction import ExtractedItem, ExtractedQuote

logger = logging.getLogger("hamdaz.comparison")

#: Column headings, in the order they are tried. Longest and most specific
#: first: "unit price" must win over "price", or every amount column collides.
_COLUMNS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "unit_price",
        ("unit price", "unit rate", "rate/unit", "price/unit", "unit cost", "u/price", "rate"),
    ),
    (
        "line_total",
        ("line total", "total price", "total amount", "extended", "amount", "net value",
         "total"),
    ),
    ("quantity", ("quantity", "qty", "qnty", "nos", "pcs", "req qty")),
    (
        "part_number",
        ("part number", "part no", "part#", "model", "mpn", "sku", "item code",
         "material code", "catalogue", "catalog"),
    ),
    ("brand", ("brand", "make", "manufacturer")),
    ("unit", ("uom", "unit of measure", "unit")),
    ("lead_time", ("lead time", "delivery time", "delivery")),
    (
        "description",
        ("description", "item description", "material description", "particulars",
         "product", "material", "item", "details"),
    ),
)

#: A row whose description matches one of these is a summary line, not an item.
#: Counting them as items would double the total and invent a "Subtotal" product.
_NOT_AN_ITEM: Final = re.compile(
    r"^\s*(sub\s*-?\s*total|total|grand\s+total|net\s+total|vat|tax|gst|freight|"
    r"shipping|discount|rounding|amount\s+in\s+words|s\.?\s*no\.?|sr\.?\s*no\.?)\b",
    re.I,
)

_LABEL = r"[:\-–]?\s*"

#: Labelled fields, read from the document's text rather than its table.
_FIELDS: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "quote_number": (
        re.compile(
            rf"(?:quotation|quote|offer|proforma|pi)\s*(?:no|number|ref|#)\.?"
            rf"{_LABEL}([A-Z0-9][A-Z0-9/\-_]{{2,}})",
            re.I,
        ),
        re.compile(rf"\b(?:ref|reference)\.?{_LABEL}([A-Z]{{2,}}[A-Z0-9/\-]{{3,}})", re.I),
    ),
    "quote_date": (
        re.compile(
            rf"(?:quote|quotation|offer|document)?\s*date{_LABEL}"
            rf"([0-9]{{1,2}}[-/. ][A-Za-z0-9]{{2,9}}[-/. ][0-9]{{2,4}})",
            re.I,
        ),
    ),
    "validity": (
        re.compile(rf"valid(?:ity|\s+until|\s+for|\s+till)?{_LABEL}([^\n]{{2,60}})", re.I),
        re.compile(rf"offer\s+valid[^\n:]*{_LABEL}([^\n]{{2,60}})", re.I),
    ),
    "delivery_time": (
        re.compile(
            rf"delivery\s*(?:time|period|lead\s*time|schedule)?{_LABEL}([^\n]{{2,60}})",
            re.I,
        ),
        re.compile(rf"lead\s*time{_LABEL}([^\n]{{2,60}})", re.I),
    ),
    "payment_terms": (
        re.compile(rf"payment\s*(?:terms|term|condition[s]?)?{_LABEL}([^\n]{{2,80}})", re.I),
        re.compile(rf"\bterms\s+of\s+payment{_LABEL}([^\n]{{2,80}})", re.I),
    ),
    "warranty": (
        re.compile(rf"warranty|guarantee{_LABEL}([^\n]{{2,60}})", re.I),
    ),
    "incoterms": (
        re.compile(r"\b(EXW|FOB|CIF|CFR|CIP|CPT|DAP|DDP|DPU|FCA|FAS)\b(?:\s+[A-Z][a-z]+)?"),
    ),
    "contact": (
        re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"),
    ),
}

#: Anchored to the start of a line. Unanchored, "from" matched inside
#: "30 days from date of offer" and made that the supplier's name.
_SUPPLIER = (
    re.compile(
        rf"^\s*(?:supplier|vendor|company|issued\s+by)\s*(?:name)?{_LABEL}([^\n]{{2,80}})",
        re.I | re.M,
    ),
)

#: Legal suffixes that mark a line as a company name on an unlabelled letterhead.
_COMPANY_SUFFIX: Final = re.compile(
    r"\b(l\.?l\.?c|fz[ce]|fzco|ltd|limited|inc|co\.?|company|corp(?:oration)?|"
    r"trading|est(?:ablishment)?|gmbh|s\.?a\.?r\.?l|pvt|private)\b\.?\s*$",
    re.I,
)

_CURRENCY_CODE: Final = re.compile(
    r"\b(AED|USD|EUR|GBP|SAR|QAR|OMR|KWD|BHD|INR|JPY|CNY|CHF|AUD|CAD|SGD)\b"
)
_CURRENCY_SYMBOL: Final = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}

#: Labels whose amount is the last number on their line. Matching the *first*
#: number instead reads "VAT 5% ... 77.00" as a tax of 5.
_TOTALS: Final[dict[str, re.Pattern[str]]] = {
    "quoted_total": re.compile(
        r"(?:grand\s+total|total\s+amount|net\s+(?:total|amount)|total\s+due)\b", re.I
    ),
    "discount": re.compile(r"\bdiscount\b", re.I),
    "freight": re.compile(r"\b(?:freight|shipping|delivery\s+charge)\b", re.I),
    "tax": re.compile(r"\b(?:vat|tax|gst)\b", re.I),
}

#: A number that is not a percentage. "5%" on a VAT line is the rate, and the
#: amount is somewhere to its right.
_AMOUNT: Final = re.compile(r"([0-9][0-9,.\s]*[0-9]|[0-9])(?!\s*%)")

#: Below this share of table rows yielding a priced item, the column mapping was
#: probably wrong. Hand it to the model rather than report a partial quote.
_MIN_ROW_YIELD: Final = 0.4


def to_number(raw: str | None) -> Decimal | None:
    """A price from a cell, or ``None``.

    Handles what quotes actually contain: currency symbols and codes, thousands
    separators in either convention (``1,234.56`` and ``1.234,56``), spaces used
    as separators, trailing minus signs, and parenthesised negatives. Guessing
    wrong here silently changes a price by a factor of a thousand, so anything
    ambiguous returns ``None`` instead.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = _CURRENCY_CODE.sub("", text)
    for symbol in _CURRENCY_SYMBOL:
        text = text.replace(symbol, "")
    text = text.replace(" ", " ").strip()
    if text.endswith("-"):
        negative, text = True, text[:-1]
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text or not any(c.isdigit() for c in text):
        return None

    if "," in text and "." in text:
        # Whichever appears last is the decimal point.
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        text = text.replace(thousands, "").replace(decimal_sep, ".")
    elif "," in text:
        parts = text.split(",")
        # "1,234" is thousands; "12,50" is a decimal comma. Three trailing
        # digits in the last group is the giveaway.
        if len(parts[-1]) == 3 and len(parts) > 1 and all(p for p in parts[:-1]):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return -value if negative else value


def _norm(cell: str) -> str:
    return re.sub(r"\s+", " ", str(cell or "")).strip().casefold()


def _map_columns(header: list[str]) -> dict[str, int]:
    """Heading text to column index, each column claimed at most once."""
    cells = [_norm(c) for c in header]
    mapping: dict[str, int] = {}
    taken: set[int] = set()

    for field, patterns in _COLUMNS:
        for pattern in patterns:
            for index, cell in enumerate(cells):
                if index in taken or not cell:
                    continue
                if cell == pattern or pattern in cell:
                    mapping[field] = index
                    taken.add(index)
                    break
            if field in mapping:
                break
    return mapping


def _find_header(table: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    """The header row and its column mapping.

    Searched rather than assumed: quotes put a letterhead, an address and a
    reference block above the table, so row 0 is rarely the header.
    """
    for index, row in enumerate(table[:8]):
        mapping = _map_columns(row)
        # A description alone is a paragraph; a description with a price is a
        # quote table.
        if "description" in mapping and ("unit_price" in mapping or "line_total" in mapping):
            return index, mapping
    return None


def _items_from_table(table: list[list[str]]) -> list[ExtractedItem]:
    found = _find_header(table)
    if not found:
        return []
    header_at, columns = found

    def cell(row: list[str], field: str) -> str | None:
        index = columns.get(field)
        if index is None or index >= len(row):
            return None
        return (row[index] or "").strip() or None

    items: list[ExtractedItem] = []
    considered = 0
    for row in table[header_at + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        considered += 1

        description = cell(row, "description")
        if not description or _NOT_AN_ITEM.match(description):
            continue

        unit_price = to_number(cell(row, "unit_price"))
        line_total = to_number(cell(row, "line_total"))
        quantity = to_number(cell(row, "quantity")) or Decimal(1)

        if unit_price is None and line_total is None:
            # A description with no money on it is a heading or a note.
            continue
        if unit_price is None and quantity:
            unit_price = line_total / quantity if quantity else line_total

        items.append(
            ExtractedItem(
                description=re.sub(r"\s+", " ", description)[:2000],
                # "" rather than None throughout: the model schema uses blanks
                # to keep its decoding grammar simple, and both readers must
                # produce the same shape.
                part_number=cell(row, "part_number") or "",
                brand=cell(row, "brand") or "",
                unit=cell(row, "unit") or "",
                quantity=float(quantity),
                unit_price=float(unit_price or 0),
                line_total=float(line_total) if line_total is not None else 0,
                lead_time=cell(row, "lead_time") or "",
            )
        )

    # A mapping that only explains a handful of rows was probably the wrong
    # mapping. Decline rather than report a fraction of the quote as the whole.
    if considered and len(items) / considered < _MIN_ROW_YIELD:
        logger.info("table yielded %d items from %d rows; declining", len(items), considered)
        return []
    return items


def _amount_on_label_line(text: str, label: re.Pattern[str]) -> Decimal | None:
    """The money on the line carrying ``label``.

    The *last* number on the line, not the first: a total row reads
    ``VAT 5% | 77.00``, and the figure that matters is on the right. Percentages
    are skipped outright, which is what stops a 5% rate being recorded as a
    5-dirham tax.
    """
    for line in text.splitlines():
        if not label.search(line):
            continue
        after = line[label.search(line).end() :]
        candidates = [
            value
            for raw in _AMOUNT.findall(after)
            if (value := to_number(raw)) is not None
        ]
        if candidates:
            return candidates[-1]
    return None


def _first(text: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    for pattern in patterns:
        if match := pattern.search(text):
            value = match.group(1).strip(" .;,:\t")
            if value:
                return value[:200]
    return None


def _supplier_name(text: str) -> str | None:
    if labelled := _first(text, _SUPPLIER):
        return labelled
    # Otherwise the letterhead: the first line that reads like a company.
    for line in text.splitlines()[:12]:
        line = line.strip()
        if 3 < len(line) < 80 and _COMPANY_SUFFIX.search(line):
            return line
    return None


def _currency(text: str) -> str | None:
    if match := _CURRENCY_CODE.search(text):
        return match.group(1).upper()
    for symbol, code in _CURRENCY_SYMBOL.items():
        if symbol in text:
            return code
    return None


def parse(readable: Readable) -> ExtractedQuote | None:
    """A quote read locally, or ``None`` to say the model should do it.

    ``None`` is the honest answer for a scan, a layout with no recognisable
    price table, or a table this could only partly explain.
    """
    if readable.kind != "text" or not readable.text:
        # A scan or a photograph. There is nothing here to parse.
        return None

    text = readable.text
    items: list[ExtractedItem] = []
    for table in readable.tables:
        items.extend(_items_from_table(table))

    if not items:
        return None
    if not any((i.unit_price or 0) > 0 or (i.line_total or 0) > 0 for i in items):
        # Descriptions with no prices are not a quote.
        return None

    supplier = _supplier_name(text)
    currency = _currency(text)
    # The two a reviewer must check when they are absent: everything else can be
    # inferred from the line items, these cannot.
    missing = [
        name
        for name, value in (("supplier name", supplier), ("currency", currency))
        if not value
    ]

    quote = ExtractedQuote(
        supplier_name=supplier or readable.file_name.rsplit(".", 1)[0],
        quote_number=_first(text, _FIELDS["quote_number"]) or "",
        quote_date=_first(text, _FIELDS["quote_date"]) or "",
        currency=currency or "",
        validity=_first(text, _FIELDS["validity"]) or "",
        delivery_time=_first(text, _FIELDS["delivery_time"]) or "",
        payment_terms=_first(text, _FIELDS["payment_terms"]) or "",
        warranty=_first(text, _FIELDS["warranty"]) or "",
        incoterms=_first(text, _FIELDS["incoterms"]) or "",
        contact=_first(text, _FIELDS["contact"]) or "",
        items=items,
        # Every field is required on the model — see the note in extraction.py
        # about optional fields and the decoding grammar. 0 and "" mean "not on
        # the document", and the totals below overwrite these where found.
        discount=0,
        freight=0,
        tax=0,
        quoted_total=0,
        note="",
    )

    for field, label in _TOTALS.items():
        if (value := _amount_on_label_line(text, label)) is not None:
            setattr(quote, field, float(value))

    notes = [f"Read without AI, directly from the document. {len(items)} line items found."]
    if missing:
        # Said plainly, because a reviewer should check these two first.
        notes.append(f"Could not find the {' or '.join(missing)}; please confirm.")
    quote.note = " ".join(notes)

    logger.info(
        "parsed %s locally: %d items, supplier=%r currency=%s",
        readable.file_name,
        len(items),
        quote.supplier_name,
        quote.currency,
    )
    return quote
