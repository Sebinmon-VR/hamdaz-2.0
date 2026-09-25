"""Reading the other documents: what an RFQ, a PO or a courier quote can tell
the quote, offered as suggestions.

A supplier quotation is read by the comparison module into prices. Every other
kind of document is read here, by its kind, for the handful of facts it
carries that belong on the quote:

* **A customer RFQ** — their reference, the closing date, where the goods go,
  the Incoterm demanded, how long the bid must stand, the tax and the duty.
  Never its items: the lines come from the supplier's quotation only.
* **An end user's PO** — the PO number, its date and its value.
* **A courier or freight quote** — the carrier, the figure and the transit
  time, offered as a landed-cost row.
* **Everything else** is filed and read for nothing; a datasheet has no field
  on the quote.

Nothing read is written onto the quote by this module. It becomes
``document.suggestions``: one entry per quote field, with the value, the page
it was read from and what the quote currently says, and :func:`apply` writes
the ones a person accepts. A suggestion fills a blank; overwriting a typed
answer is the person's decision, said explicitly.

Deterministic, the way the supplier-quote parser is: labelled fields and
tables, found by the words next to them. A model can be asked for the rest —
the hook is :func:`_second_pass` — and answers through the same suggestions,
so a field arrives the same way whichever reader found it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from app.comparison import parsing
from app.comparison.documents import DocumentError, Readable, prepare
from app.core.llm import TextModel
from app.models.quoting import (
    CostStage,
    DocumentKind,
    QuoteCostLine,
    QuoteDocument,
    QuoteRequest,
)
from app.quoting.service import QuoteError

logger = logging.getLogger("hamdaz.quoting")

_LABEL: Final = r"\s*(?:no|number|ref|reference|#)?\.?\s*[:\-–]?\s*"

_DATE: Final = (
    r"(\d{1,2}[-/. ]\d{1,2}[-/. ]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4}"
    r"|[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
)

#: The words that name a field, per kind. Each pattern's first group is the value.
_RFQ: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "reference_number": (
        re.compile(
            rf"\b(?:RFQ|RFP|RFT|RFI|EOI|tender|enquiry|inquiry|event|PR)"
            rf"{_LABEL}([A-Z]{{0,4}}[-/]?\d{{4,}}[A-Z0-9\-/]*)",
            re.I,
        ),
    ),
    "cf_bcd": (
        re.compile(
            rf"(?:closing|bid\s+close|due|submission|deadline|last)"
            rf"[^\n:]{{0,30}}[:\-–]?\s*{_DATE}",
            re.I,
        ),
    ),
    "requested_delivery_date": (
        re.compile(
            rf"(?:required|requested|expected)\s+delivery[^\n:]{{0,20}}[:\-–]?\s*{_DATE}",
            re.I,
        ),
    ),
    "ship_to": (
        re.compile(
            r"(?:deliver(?:y)?\s+(?:to|location|address|point|place)|ship\s+to|"
            r"place\s+of\s+(?:supply|delivery))\s*[:\-–]?\s*([^\n]{3,90})",
            re.I,
        ),
    ),
    "incoterm_required": (
        re.compile(
            r"\b(EXW|FOB|CIF|CFR|CIP|CPT|DAP|DDP|DPU|FCA|FAS)\b(?:\s+([A-Z][A-Za-z .]{2,40}))?",
        ),
    ),
    "bid_validity_days": (
        re.compile(
            r"(?:bid|offer|quote|quotation|price)\s+validity[^\n:]{0,20}[:\-–]?\s*(\d{1,3})\s*days",
            re.I,
        ),
        re.compile(r"valid(?:ity)?\s+(?:for|period)?\s*[:\-–]?\s*(\d{1,3})\s*days", re.I),
    ),
    # The tax the buyer expects on the price, and the duty they expect us to
    # carry. "Prices exclusive of VAT 5%", "customs duty at 5% to be included".
    "tax_percentage": (
        re.compile(r"\bVAT\b[^\n%\d]{0,20}(\d{1,2}(?:\.\d)?)\s*%", re.I),
        re.compile(r"(\d{1,2}(?:\.\d)?)\s*%\s*VAT\b", re.I),
    ),
    "customs_duty_percent": (
        re.compile(
            r"(?:customs|import)\s+dut(?:y|ies)[^\n%\d]{0,30}(\d{1,2}(?:\.\d)?)\s*%", re.I
        ),
    ),
    "payment_terms": (
        re.compile(r"payment\s*(?:terms?|conditions?)?\s*[:\-–]\s*([^\n]{3,120})", re.I),
    ),
    "delivery_terms": (
        re.compile(
            r"delivery\s*(?:terms?|period|time|schedule|lead\s*time)\s*[:\-–]\s*([^\n]{3,120})",
            re.I,
        ),
    ),
}

_PO: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "reference_number": (
        re.compile(
            rf"(?:P\.?\s?O\.?|purchase\s+order|order){_LABEL}([A-Z0-9][A-Z0-9\-/]{{3,}})", re.I
        ),
    ),
    "po_date": (
        re.compile(rf"(?:P\.?\s?O\.?|order)\s+date[^\n:]{{0,10}}[:\-–]?\s*{_DATE}", re.I),
    ),
}

_TRANSIT: Final = (
    re.compile(
        r"(?:transit|delivery|lead)\s*(?:time|period)?[^\n:]{0,10}[:\-–]?\s*([^\n]{2,50})", re.I,
    ),
)

#: The quote fields a suggestion may name, and how they read on screen.
FIELD_LABELS: Final[dict[str, str]] = {
    "reference_number": "Their reference",
    "cf_bcd": "Bid closing date",
    "requested_delivery_date": "Requested delivery date",
    "ship_to": "Ship to",
    "place_of_supply": "Place of supply",
    "incoterm_required": "Incoterm required",
    "incoterm_place": "Named place",
    "bid_validity_days": "Bid validity (days)",
    "freight": "Freight cost row",
    "po_value": "PO value",
    "po_date": "PO date",
    "tax_percentage": "VAT on the total",
    "customs_duty_percent": "Customs duty",
    "payment_terms": "Payment terms",
    "delivery_terms": "Delivery terms",
}

_DATE_FIELDS: Final = frozenset({"cf_bcd", "requested_delivery_date", "po_date"})
_INT_FIELDS: Final = frozenset({"bid_validity_days"})
_DECIMAL_FIELDS: Final = frozenset({"tax_percentage", "customs_duty_percent"})


# ── reading ────────────────────────────────────────────────────────────


async def read_into(
    document: QuoteDocument,
    request: QuoteRequest,
    file_name: str,
    content: bytes,
    content_type: str | None,
    *,
    model: TextModel | None = None,
) -> None:
    """Read the file for its kind and put what was found on the document row.

    Never raises: a document that cannot be read is still filed, with a note
    saying it was not read. Reading is a convenience on top of filing.
    """
    try:
        # In a thread: pdfplumber on a long PDF, and OCR on a scan, take
        # seconds to a minute, and the server has other requests to answer.
        readable = await asyncio.to_thread(prepare, file_name, content, content_type)
    except DocumentError as exc:
        document.extracted = {"read": False, "why": str(exc)}
        return
    if readable.kind != "text" or not readable.text:
        document.extracted = {
            "read": False,
            "why": "No text to read — a scan or a photograph. Filed as it is.",
        }
        return

    kind = DocumentKind(document.kind)
    if kind is DocumentKind.CUSTOMER_RFQ:
        found = _read_rfq(readable)
    elif kind is DocumentKind.END_USER_PO:
        found = _read_po(readable)
    elif kind is DocumentKind.FREIGHT_QUOTE:
        found = _read_freight(readable)
    else:
        document.extracted = {"read": True, "fields": {}, "pages": _page_count(readable.text)}
        return

    found = await _second_pass(readable, kind, found, model)
    document.extracted = {
        "read": True,
        "pages": _page_count(readable.text),
        "fields": {k: v for k, v in found.items()},
    }
    document.suggestions = _suggest(found, request)
    logger.info("quote %s: read %s from %s", request.id, sorted(found), file_name)


def _read_rfq(readable: Readable) -> dict[str, dict[str, Any]]:
    text = readable.text or ""
    found: dict[str, dict[str, Any]] = {}
    for field, patterns in _RFQ.items():
        hit = _first(text, patterns)
        if hit is None:
            continue
        value, page = hit
        if field == "incoterm_required":
            term, _, place = value.partition(" ")
            found[field] = {"value": term.upper(), "page": page}
            if place.strip():
                found["incoterm_place"] = {"value": place.strip()[:200], "page": page}
            continue
        found[field] = {"value": _clean(field, value), "page": page}
    if "ship_to" in found and "place_of_supply" not in found:
        # The RFQ's delivery place is the quote's place of supply too, unless
        # somebody says otherwise. Offered as its own suggestion.
        found["place_of_supply"] = {**found["ship_to"]}
    # No items. What the customer asked for is not what we are selling them
    # until a supplier has priced it: the lines come from the supplier's
    # quotation and nowhere else. The RFQ gives everything around them.
    return {k: v for k, v in found.items() if v.get("value") not in (None, "", [])}


def _read_po(readable: Readable) -> dict[str, dict[str, Any]]:
    text = readable.text or ""
    found: dict[str, dict[str, Any]] = {}
    for field, patterns in _PO.items():
        hit = _first(text, patterns)
        if hit is not None:
            found[field] = {"value": _clean(field, hit[0]), "page": hit[1]}
    total = parsing._amount_on_label_line(text, parsing._TOTALS["quoted_total"])
    if total is None:
        total = parsing._amount_on_label_line(text, re.compile(r"\btotal\b", re.I))
    if total is not None:
        found["po_value"] = {
            "value": str(total),
            "currency": parsing._currency(text),
            "page": _page_of(text, str(total)),
        }
    return {k: v for k, v in found.items() if v.get("value") not in (None, "")}


def _read_freight(readable: Readable) -> dict[str, dict[str, Any]]:
    text = readable.text or ""
    total = parsing._amount_on_label_line(text, parsing._TOTALS["quoted_total"])
    if total is None:
        total = parsing._amount_on_label_line(text, re.compile(r"\btotal\b", re.I))
    if total is None:
        total = parsing._amount_on_label_line(text, parsing._TOTALS["freight"])
    if total is None:
        return {}
    carrier = parsing._supplier_name(text)
    transit = _first(text, _TRANSIT)
    return {
        "freight": {
            "value": str(total),
            "currency": parsing._currency(text),
            "carrier": carrier,
            "transit": transit[0] if transit else None,
            "page": _page_of(text, str(total)),
        }
    }


#: What a model is asked for, per kind, when the words left gaps. The keys
#: are the quote fields; the values say what to copy.
_MODEL_SHAPES: Final[dict[DocumentKind, dict[str, Any]]] = {
    DocumentKind.CUSTOMER_RFQ: {
        "reference_number": "the RFQ / tender / enquiry number as printed",
        "cf_bcd": "bid closing or submission deadline, as printed",
        "ship_to": "where the goods are to be delivered",
        "incoterm_required": "EXW, FOB, CIF, DAP, DDP… if stated",
        "incoterm_place": "the named place after the Incoterm",
        "bid_validity_days": "how many days the bid must stand, as a number",
        "requested_delivery_date": "delivery date asked for, as printed",
        "tax_percentage": "VAT rate the prices must carry, as a number, if stated",
        "customs_duty_percent": "customs duty rate we must include, as a number, if stated",
        "payment_terms": "payment terms as printed",
        "delivery_terms": "delivery terms or period as printed",
    },
    DocumentKind.END_USER_PO: {
        "reference_number": "the purchase order number",
        "po_date": "the PO date as printed",
        "po_value": "the PO total as printed",
    },
    DocumentKind.FREIGHT_QUOTE: {
        "freight": "the total freight charge as printed",
        "currency": "ISO currency code",
        "carrier": "the courier or forwarder",
        "transit": "transit or delivery time as printed",
    },
}

#: The fields whose absence is worth a model call, per kind.
_WORTH_ASKING: Final[dict[DocumentKind, tuple[str, ...]]] = {
    DocumentKind.CUSTOMER_RFQ: ("reference_number", "cf_bcd"),
    DocumentKind.END_USER_PO: ("reference_number", "po_value"),
    DocumentKind.FREIGHT_QUOTE: ("freight",),
}


def _cell(cells: list[str], at: int | None) -> str | None:
    if at is None or at >= len(cells):
        return None
    return (cells[at] or "").strip() or None


async def _second_pass(
    readable: Readable,
    kind: DocumentKind,
    found: dict[str, Any],
    model: TextModel | None,
) -> dict[str, Any]:
    """A model fills what the words did not, in the same shape.

    Called only when a model is configured and one of the fields worth having
    is missing — a regular RFQ costs no model call at all. Whatever the model
    adds is marked as the model's, and only fills gaps: the words found by
    their labels are kept over anything a model says about them.
    """
    if model is None or not model.configured:
        return found
    wanted = _WORTH_ASKING.get(kind, ())
    if all(field in found for field in wanted):
        return found
    shape = _MODEL_SHAPES.get(kind)
    if shape is None:
        return found
    answer = await model.extract(
        instructions=(
            f"Read this {kind.value.replace('_', ' ')} for a quotation team. Copy the "
            f"fields below exactly as printed."
        ),
        text=readable.text or "",
        shape=shape,
    )
    if answer is None:
        return found
    payload, who = answer
    merged = dict(found)
    for field, raw in payload.items():
        if field in merged or raw in (None, "", 0, []):
            continue
        if field == "freight":
            merged["freight"] = {
                "value": str(parsing.to_number(str(raw)) or raw),
                "currency": (payload.get("currency") or None),
                "carrier": (payload.get("carrier") or None),
                "transit": (payload.get("transit") or None),
                "page": None,
                "source": who,
            }
        elif field in FIELD_LABELS:
            value = _clean(field, str(raw))
            if value not in (None, ""):
                merged[field] = {"value": value, "page": None, "source": who}
    return merged


# ── suggestions ────────────────────────────────────────────────────────


def _suggest(found: dict[str, dict[str, Any]], request: QuoteRequest) -> dict[str, Any]:
    """What was found, as proposals against what the quote currently says."""
    out: dict[str, Any] = {}
    for field, fact in found.items():
        if field not in FIELD_LABELS:
            continue
        current = _current(request, field)
        entry: dict[str, Any] = {
            "label": FIELD_LABELS[field],
            "value": fact["value"],
            "source": (
                f"page {fact['page']}" if fact.get("page") else fact.get("source")
            ),
            "current": current,
            "applied": False,
        }
        for extra in ("currency", "carrier", "transit"):
            if fact.get(extra) is not None:
                entry[extra] = fact[extra]
        # Same as what is there already: nothing to suggest, said as agreement.
        scalar = field != "freight"
        if scalar and current is not None and _same(current, fact["value"]):
            entry["applied"] = True
        out[field] = entry
    return out


def _current(request: QuoteRequest, field: str) -> Any:
    if field == "freight":
        return len(request.cost_lines)
    if field in ("po_value", "po_date"):
        return None
    if field == "tax_percentage":
        value = request.tax_percentage
        return None if value is None else str(value)
    if field == "customs_duty_percent":
        value = request.customs_duty_percent
        return None if value is None or value == 0 else str(value)
    value = getattr(request, field, None)
    if isinstance(value, datetime | date):
        return value.strftime("%Y-%m-%d")
    return value


def _same(current: Any, proposed: Any) -> bool:
    return str(current).strip().casefold() == str(proposed).strip().casefold()


def apply(
    request: QuoteRequest, document: QuoteDocument, fields: list[str], *, overwrite: bool = False
) -> list[str]:
    """Write the named suggestions onto the quote. Returns what was written."""
    suggestions = dict(document.suggestions or {})
    if not suggestions:
        raise QuoteError("This document has nothing to suggest.")
    applied: list[str] = []
    for field in fields:
        entry = suggestions.get(field)
        if entry is None:
            raise QuoteError(f"{field!r} is not something this document suggested.")
        value = entry["value"]
        if field == "freight":
            carrier = entry.get("carrier")
            currency = (entry.get("currency") or "").upper() or None
            ours = (request.currency or "").upper()
            row = QuoteCostLine(
                position=len(request.cost_lines) + 1,
                stage=CostStage.ORIGIN,
                label=f"Freight – {carrier}" if carrier else "Freight",
                basis="Courier quotation"
                + (f", {entry['transit']}" if entry.get("transit") else ""),
                amount_source=Decimal(str(value)) if currency and currency != ours else None,
                source_currency=currency if currency and currency != ours else None,
                amount_base=Decimal(str(value)) if not currency or currency == ours else Decimal(0),
                is_firm=True,
                notes=f"From {document.file_name}",
            )
            request.cost_lines.append(row)
        elif field in ("po_value", "po_date"):
            # Facts about the order, kept on the document; the quote has no
            # column for them and a note is where a person looks.
            note = f"{FIELD_LABELS[field]}: {value}"
            request.notes = f"{request.notes}\n{note}" if request.notes else note
        elif field == "tax_percentage":
            from app.core.config import get_settings
            from app.quoting import costing

            if _current(request, field) not in (None, "", 0) and not overwrite:
                raise QuoteError(
                    "The quote already carries a tax. Tick 'replace what is there' to "
                    "change it."
                )
            costing.set_tax(
                request, Decimal(str(value)), get_settings().costing_default_tax_name
            )
        else:
            current = getattr(request, field, None)
            if current not in (None, "", 0) and not overwrite:
                raise QuoteError(
                    f"{FIELD_LABELS[field]} is already filled in. Tick 'replace what is "
                    f"there' to write over it."
                )
            setattr(request, field, _typed(field, value))
        entry = {**entry, "applied": True}
        suggestions[field] = entry
        applied.append(field)
    # Reassigned rather than mutated: a JSONB column only sees a new value.
    document.suggestions = suggestions
    return applied


# ── helpers ────────────────────────────────────────────────────────────


def _first(text: str, patterns: tuple[re.Pattern[str], ...]) -> tuple[str, int | None] | None:
    for pattern in patterns:
        if match := pattern.search(text):
            value = " ".join(g for g in match.groups() if g).strip(" .;,:\t")
            if value:
                return value[:200], _page_at(text, match.start())
    return None


def _page_at(text: str, position: int) -> int | None:
    """Which page a character offset falls on, from the reader's markers."""
    page = None
    for marker in re.finditer(r"--- page (\d+) ---", text):
        if marker.start() > position:
            break
        page = int(marker.group(1))
    return page


def _page_of(text: str, needle: str) -> int | None:
    at = text.find(needle)
    return _page_at(text, at) if at >= 0 else None


def _page_count(text: str) -> int:
    return max(1, len(re.findall(r"--- page \d+ ---", text)))


def _clean(field: str, raw: str) -> Any:
    value = raw.strip()
    if field in _DATE_FIELDS:
        parsed = parse_date(value)
        return parsed.isoformat() if parsed else None
    if field in _INT_FIELDS:
        digits = re.sub(r"\D", "", value)
        return int(digits) if digits else None
    if field in _DECIMAL_FIELDS:
        number = parsing.to_number(value)
        return str(number.normalize()) if number is not None else None
    return value[:200]


def _typed(field: str, value: Any) -> Any:
    if field in _DATE_FIELDS:
        return date.fromisoformat(str(value))
    if field in _INT_FIELDS:
        return int(value)
    if field in _DECIMAL_FIELDS:
        return Decimal(str(value))
    return str(value)


_DATE_FORMATS: Final = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %m %Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d %b, %Y", "%b %d, %Y", "%B %d, %Y",
)


def parse_date(raw: str) -> date | None:
    """A date as people print them, or ``None``. Day first, as the Gulf writes
    it: 04/10/2026 is the fourth of October."""
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw.strip())
    text = re.sub(r"\s+", " ", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
