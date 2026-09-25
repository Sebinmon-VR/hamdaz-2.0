"""Reading a supplier quote without a model.

Most supplier quotes are machine-generated and thoroughly regular: a table with
a description column and a price column, and a handful of labelled fields
around it. Parsing that is deterministic work, and deterministic work should
not cost money or vary between runs. This is the only reader there is.

Three ways in, tried in order:

1. **A table with headings.** Whatever ``documents.py`` found — a ruled grid, a
   spreadsheet, or a grid rebuilt from word positions — is read by mapping its
   headings to fields and walking the rows.
2. **Lines shaped like rows.** When no heading row can be found, a line that
   ends in a quantity and one or two amounts is read as an item. A quote pasted
   into an email, or one whose headings were an image, still reads this way.
3. Nothing. ``parse`` returns ``None`` and the extractor tells the person to
   type it in.

The design rests on one rule: **admit failure loudly**. A parser that half-works
is worse than one that declines, because a quote with three of its eight lines
found looks exactly like a quote with three lines. So the table reader declines
when its column mapping explains too few rows, and the line reader needs more
than one row that fits before it believes the shape.

What this does *not* attempt: deciding whether two suppliers' items are the same
thing. That is ``analysis.py``'s job, and it is done by the part numbers and
the words rather than by anything here.
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
        ("unit price", "unit rate", "rate/unit", "price/unit", "unit cost", "u/price",
         "price each", "each", "rate", "price"),
    ),
    (
        "line_total",
        ("line total", "total price", "total amount", "extended", "ext. price", "ext price",
         "amount", "net value", "value", "total"),
    ),
    ("quantity", ("quantity", "qty", "qnty", "nos", "pcs", "req qty")),
    (
        "part_number",
        ("part number", "part no", "part#", "part", "model", "mpn", "sku", "item code",
         "material code", "catalogue", "catalog", "code", "article"),
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
    r"shipping|delivery\s+charge|handling|discount|rounding|amount\s+in\s+words|"
    r"s\.?\s*no\.?|sr\.?\s*no\.?)\b",
    re.I,
)

_LABEL = r"[:\-–]?\s*"

#: Labelled fields, read from the document's text rather than its table.
_FIELDS: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "quote_number": (
        re.compile(
            rf"(?:quotation|quote|offer|proforma|pi|order)\s*(?:no|number|ref|#)\.?"
            rf"{_LABEL}([A-Z0-9][A-Z0-9/\-_]{{2,}})",
            re.I,
        ),
        re.compile(rf"\b(?:ref|reference)\.?{_LABEL}([A-Z]{{2,}}[A-Z0-9/\-]{{3,}})", re.I),
    ),
    "quote_date": (
        re.compile(
            rf"(?:quote|quotation|offer|document|order)?\s*date{_LABEL}"
            rf"([0-9]{{1,2}}[-/. ][A-Za-z0-9]{{2,9}}[-/. ][0-9]{{2,4}})",
            re.I,
        ),
        re.compile(
            rf"(?:quote|quotation|offer|document|order)?\s*date{_LABEL}"
            rf"([A-Za-z]{{3,9}}\s+[0-9]{{1,2}},?\s+[0-9]{{4}})",
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
        re.compile(rf"(?:warranty|guarantee){_LABEL}([^\n]{{2,60}})", re.I),
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
        rf"^\s*(?:supplier|vendor|company|issued\s+by|sold\s+by|from)\s*(?:name)?"
        rf"{_LABEL}([^\n]{{2,80}})",
        re.I | re.M,
    ),
)

#: Legal suffixes that mark a line as a company name on an unlabelled letterhead.
_COMPANY_SUFFIX: Final = re.compile(
    r"\b(l\.?l\.?c|fz[ce]|fzco|ltd|limited|inc|co\.?|company|corp(?:oration)?|"
    r"trading|est(?:ablishment)?|gmbh|s\.?a\.?r\.?l|pvt|private|plc|b\.?v\.?)\b\.?\s*$",
    re.I,
)

#: A web shop names itself by its domain and nothing else.
_DOMAIN_LINE: Final = re.compile(
    r"^\s*(?:www\.)?([A-Za-z0-9][A-Za-z0-9\-]{1,40}\.(?:com|net|org|ae|co\.uk|de|in|io))"
    r"\s*$",
    re.I,
)

#: Words that mean a line is a heading or a label, not a name.
_NOT_A_NAME: Final = re.compile(
    r"\b(quot|invoice|proforma|date|tel|phone|fax|e-?mail|page|to:|attn|dear|"
    r"description|item|qty|price|amount|total|ref|po box|www)\b|[:|]",
    re.I,
)

_CURRENCY_CODE: Final = re.compile(
    r"\b(AED|USD|EUR|GBP|SAR|QAR|OMR|KWD|BHD|INR|JPY|CNY|CHF|AUD|CAD|SGD)\b"
)
_CURRENCY_SYMBOL: Final = {
    "US$": "USD", "$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY",
    "Dhs": "AED", "dhs": "AED", "DH": "AED",
}

#: Labels whose amount is the last number on their line. Matching the *first*
#: number instead reads "VAT 5% ... 77.00" as a tax of 5.
_TOTALS: Final[dict[str, re.Pattern[str]]] = {
    "quoted_total": re.compile(
        r"(?:grand\s+total|total\s+amount|net\s+(?:total|amount)|total\s+due|"
        r"order\s+total|amount\s+payable)\b",
        re.I,
    ),
    "discount": re.compile(r"\bdiscount\b", re.I),
    "freight": re.compile(r"\b(?:freight|shipping|delivery\s+charge|courier)\b", re.I),
    "tax": re.compile(r"\b(?:vat|tax|gst)\b", re.I),
}

#: A number that is not a percentage. "5%" on a VAT line is the rate, and the
#: amount is somewhere to its right.
_AMOUNT: Final = re.compile(r"([0-9][0-9,.\s]*[0-9]|[0-9])(?!\s*%)")

#: Below this share of table rows yielding a priced item, the column mapping was
#: probably wrong. Decline rather than report a partial quote as the whole.
_MIN_ROW_YIELD: Final = 0.4

#: A line shaped like a row of a quote: something, then a quantity, then one or
#: two amounts, and nothing after. The quantity is a whole number or a short
#: decimal; the amounts carry cents or thousands separators, which is what tells
#: "10 120.00" apart from a part number.
_MONEY = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,4})?"
#: A description with one of these in it is a label, an address or an email
#: line, whatever numbers follow it. OCR'd letterheads produce plenty.
_NOT_A_DESCRIPTION: Final = re.compile(r"[:|@%]|\b(?:tel|fax|phone|e-?mail|www|http)\b", re.I)
#: On a row with no line total there is no arithmetic to check, so the price
#: must at least look like money: cents, or a thousands separator.
_LOOKS_LIKE_MONEY: Final = re.compile(r"\.\d{2}$|,\d{3}")

#: A price typed into an email: "Black Cartridge- 410A (CF410A)@300",
#: "Filter FX-200 : AED 120.00", "Seal kit – 85.00 each". The separator
#: carries the meaning — an "@" is unambiguous and takes a bare number; a
#: colon or a dash could be anything, so the number then has to look like
#: money.
_AT_PRICE_LINE: Final = re.compile(
    rf"^\s*(?:[-•*·]\s*)?(?P<desc>[^@:\n]{{3,120}}?)\s*(?P<sep>@|:|-|–|=)\s*"
    rf"(?:(?P<cur>[A-Z]{{3}}|Dhs|US\$|\$|€|£)\s*)?(?P<price>{_MONEY})"
    rf"\s*(?:/\s*(?:each|unit|pc|pcs|nos))?\s*$"
)
#: The enquiry quoted under an emailed reply — "Black Cartridge- 410A
#: (CF410A)-1 Unit" — is where the quantities are.
_ENQUIRY_QTY: Final = re.compile(
    r"^\s*(?:[-•*·]\s*)?(?P<desc>.{3,120}?)\s*[-–:x×]\s*(?P<qty>\d{1,6})\s*"
    r"(?:units?|nos|pcs?|pieces?|each|sets?)\b",
    re.I,
)
#: Words that start a label rather than an item, on a line that happens to
#: end in a number.
_LABEL_WORDS: Final = re.compile(
    r"^(?:date|from|to|cc|subject|re|ref|reference|tel|phone|fax|e-?mail|page|"
    r"validity|valid|delivery|payment|terms|quote|quotation|offer|note|notes|"
    r"regards|dear|hello|hi|thanks?)\b",
    re.I,
)
#: A manufacturer code in brackets — "(CF410A)".
_BRACKETED_CODE: Final = re.compile(r"\(([A-Z0-9][A-Z0-9\-/]{3,})\)")
_ROW_LINE: Final = re.compile(
    rf"^\s*(?:(?P<n>\d{{1,3}})[.)]?\s+)?(?P<desc>\S.*?\S)\s+"
    rf"(?P<qty>\d{{1,6}}(?:\.\d{{1,3}})?)\s*(?P<unit>[A-Za-z]{{1,6}})?\s+"
    rf"(?:[A-Z]{{3}}\s*|[$€£]\s*)?(?P<price>{_MONEY})"
    rf"(?:\s+(?:[A-Z]{{3}}\s*|[$€£]\s*)?(?P<total>{_MONEY}))?\s*$"
)


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
    text = text.replace(" ", " ").strip()
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
        # A part-number column with a price is a quote table too — many web
        # shops print the SKU and never say "description".
        if (
            "part_number" in mapping
            and "description" not in mapping
            and ("unit_price" in mapping or "line_total" in mapping)
        ):
            mapping["description"] = mapping.pop("part_number")
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
                # "" rather than None throughout: the extracted shape uses
                # blanks to mean "not on the document".
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


def _items_from_lines(text: str) -> list[ExtractedItem]:
    """Rows read from the shape of the lines, when no heading row was found.

    A line is an item when it ends in a quantity and an amount, or a quantity,
    a unit price and a line total. Two amounts must agree with the quantity —
    ``10 × 120.00 = 1,200.00`` — or the line is left alone: a line that happens
    to end in three numbers is more often a date and a phone number than a
    price. With one amount there is no such check, so at least two such lines
    are needed before any of them is believed.
    """
    items: list[ExtractedItem] = []
    weak = 0
    for raw in text.splitlines():
        line = raw.strip().strip("|").strip()
        if not line or _NOT_AN_ITEM.match(line):
            continue
        match = _ROW_LINE.match(line)
        if not match:
            continue
        description = match.group("desc").strip(" |-–:")
        if not description or _NOT_AN_ITEM.match(description) or len(description) < 3:
            continue
        if _NOT_A_DESCRIPTION.search(description):
            continue
        quantity = to_number(match.group("qty"))
        price = to_number(match.group("price"))
        total = to_number(match.group("total")) if match.group("total") else None
        if quantity is None or price is None or quantity <= 0:
            continue
        if total is not None:
            if abs(quantity * price - total) > max(Decimal("0.05"), total * Decimal("0.01")):
                # Three numbers that do not multiply are not qty, price, total.
                continue
        else:
            if not _LOOKS_LIKE_MONEY.search(match.group("price")):
                # "Prepared 24 Sep 2026" ends in a number too.
                continue
            weak += 1
        part = ""
        # A leading token that reads like a code — "881457-B21 HPE 2.4TB ..." —
        # is the part number, and the rest is the description.
        head, _, rest = description.partition(" ")
        looks_like_code = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_/.]{2,}", head)
        if rest and re.search(r"\d", head) and looks_like_code:
            part, description = head, rest
        items.append(
            ExtractedItem(
                description=re.sub(r"\s+", " ", description)[:2000],
                part_number=part,
                brand="",
                unit=(match.group("unit") or ""),
                quantity=float(quantity),
                unit_price=float(price),
                line_total=float(total) if total is not None else 0,
                lead_time="",
            )
        )
    if not items or (weak == len(items) and len(items) < 2):
        return _items_from_at_prices(text)
    return items


def _items_from_at_prices(text: str) -> list[ExtractedItem]:
    """A quotation typed into an email, one "item @ price" per line.

    No table, no headings, no quantity column: the supplier answered the
    enquiry by writing a price after each item. At least two such lines are
    needed before the shape is believed, and a line whose separator is a
    colon or a dash only counts when the figure looks like money — "Tel: 971"
    and "Date: 22" are not prices.

    The quantities are read from the enquiry quoted underneath, where the
    person asked for "-1 Unit" of each. A line with no such answer is one
    unit, and the note says so.
    """
    items: list[ExtractedItem] = []
    lines = [line.strip().strip("|").strip() for line in text.splitlines()]
    for line in lines:
        if not line or _NOT_AN_ITEM.match(line):
            continue
        match = _AT_PRICE_LINE.match(line)
        if not match:
            continue
        description = match.group("desc").strip(" -–:")
        if len(description) < 3 or _LABEL_WORDS.match(description):
            continue
        if _NOT_A_DESCRIPTION.search(description) and match.group("sep") != "@":
            continue
        if match.group("sep") != "@" and not _LOOKS_LIKE_MONEY.search(match.group("price")):
            continue
        price = to_number(match.group("price"))
        if price is None or price <= 0:
            continue
        code = _BRACKETED_CODE.search(description)
        items.append(
            ExtractedItem(
                description=re.sub(r"\s+", " ", description)[:2000],
                part_number=code.group(1) if code else "",
                brand="",
                unit="",
                quantity=1,
                unit_price=float(price),
                line_total=0,
                lead_time="",
            )
        )
    if len(items) < 2:
        return []

    # The enquiry underneath: the same items, with what was asked for.
    asked: dict[str, float] = {}
    for line in lines:
        match = _ENQUIRY_QTY.match(line)
        if match:
            asked[_squash(match.group("desc"))] = float(match.group("qty"))
    for item in items:
        key = _squash(item.description)
        for wanted, qty in asked.items():
            if wanted.startswith(key) or key.startswith(wanted):
                item.quantity = qty
                break
    return items


def _squash(text: str) -> str:
    """A description reduced to what two spellings of it share."""
    return re.sub(r"[^a-z0-9]", "", text.casefold())


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
        # "From Yalla LLC <info@yallallc.com>": the name, not the address.
        return labelled.split("<")[0].strip(" :-") or labelled
    head = [line.strip() for line in text.splitlines()[:14] if line.strip()]
    # The letterhead: the first line that reads like a company.
    for line in head:
        if 3 < len(line) < 80 and _COMPANY_SUFFIX.search(line):
            return line
    # A web shop: a bare domain name at the top of the page.
    for line in head:
        if match := _DOMAIN_LINE.match(line):
            return match.group(1)
    # The first line, when it is a name and not a heading: "Router-Switch.com",
    # "Al Masaood Bergum" — short, mostly letters, nothing a label would say.
    for line in head[:2]:
        if line.startswith("---"):
            continue
        letters = sum(c.isalpha() for c in line)
        if 3 < len(line) < 60 and letters >= len(line) * 0.6 and not _NOT_A_NAME.search(line):
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
    """A quote read locally, or ``None`` when there is nothing here to read.

    ``None`` is the honest answer for a scan that was not OCR'd, a layout with
    no recognisable price table, or a table this could only partly explain.
    """
    if readable.kind != "text" or not readable.text:
        # A scan or a photograph. There is nothing here to parse.
        return None

    text = readable.text
    items: list[ExtractedItem] = []
    for table in readable.tables:
        items.extend(_items_from_table(table))
    how = "from its price table"
    if not items:
        items = _items_from_lines(text)
        how = "from the shape of its lines"

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
        # 0 and "" mean "not on the document", and the totals below overwrite
        # these where found.
        discount=0,
        freight=0,
        tax=0,
        quoted_total=0,
        note="",
    )

    for field, label in _TOTALS.items():
        if (value := _amount_on_label_line(text, label)) is not None:
            setattr(quote, field, float(value))

    notes = [
        f"Read without AI, directly from the document {how}. {len(items)} line items found."
    ]
    if how == "from the shape of its lines" and all(
        (i.quantity or 0) == 1 and not i.line_total for i in items
    ):
        notes.append("No quantities were printed beside the prices; each line is one unit.")
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
