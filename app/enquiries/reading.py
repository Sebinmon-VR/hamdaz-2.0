"""Reading a tender's documents into requirement lines.

The customer's documents are anything — a tender PDF of forty pages, an RFQ
mail printed to PDF, a bill of quantities in Excel, a scanned drawing — and
they are read in **one** model call, so an item listed in the BOQ and specified
in the tender is one line, not two.

What reaches the model is as little as will do. A document with a text layer
(and every spreadsheet, Word file and CSV) is read here and sent as text; only
a scan or a photograph goes as the file itself, for the model to look at. Text
is cut to a budget per document, head and tail, which is where the item tables
and the conditions are; a tender's middle is boilerplate.

Supplier quotations among the documents are **not** read here. They have a
reader of their own (``app.comparison``) that needs no model, and their lines
are offers, not requirements. Which file is which is guessed from its name
first; the model is asked to say for the ones the name did not settle, and
those it calls quotations go to the quote reader afterwards.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.comparison.documents import Readable

#: Text per document, in characters. Roughly 15k tokens: a full BOQ fits, the
#: tender's standard terms do not need to.
DOC_TEXT_BUDGET: Final = 60_000
#: Text for the whole call. Past this each document's share shrinks.
TOTAL_TEXT_BUDGET: Final = 240_000
#: Scans and photographs sent whole, by total size. A request is capped at
#: 32 MB and base64 adds a third, so 20 MB of files is what fits beside the
#: text. A tender scanned at 600 dpi is a mistake to send in full; the person
#: is told which were left out.
TOTAL_FILE_BYTES: Final = 20 * 1_048_576

#: Names that say "the customer's requirement".
_REQUIREMENT_NAME = re.compile(
    r"\b(rfq|rfp|rfi|itb|request for|invitation to|tender|enquiry|inquiry|boq|b\.o\.q|bill of quantit|specs?|"
    r"specification|scope|sow|requirement|datasheet|data sheet|drawing|annex|appendix|"
    r"technical|purchase requisition|pr\d|material request)",
    re.I,
)
#: Names that say "a supplier's offer".
_QUOTE_NAME = re.compile(
    r"\b(quotation|quote|qtn|offer|proforma|pro-forma|pi[\s_-]?\d|price ?list|commercial proposal)",
    re.I,
)
#: What this app itself files into a task folder, never read back as input.
_OWN_OUTPUT = re.compile(r"(selling\s*&?\s*costing report|enquiry analysis)", re.I)


def guess_kind(file_name: str) -> str | None:
    """``requirement``, ``supplier_quote``, or ``None`` when the name does not say.

    A requirement word wins over a quotation word: "Request for Quotation"
    is the customer's, and an RFQ is the commonest file on a task.
    """
    stem = file_name.rsplit(".", 1)[0].replace("_", " ")
    if _REQUIREMENT_NAME.search(stem):
        return "requirement"
    if _QUOTE_NAME.search(stem):
        return "supplier_quote"
    return None


def is_own_output(file_name: str, path: str | None, report_folder: str) -> bool:
    """A file this app put in the folder: a costing report, an analysis report."""
    if _OWN_OUTPUT.search(file_name):
        return True
    top = (path or "").split("/", 1)[0].strip().casefold()
    return bool(report_folder) and top == report_folder.strip().casefold() and "/" in (path or "")


# ── what is asked ──────────────────────────────────────────────────────

_ITEM: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "The item as the customer describes it, complete enough to source.",
        },
        "part_number": {"type": "string", "description": "Manufacturer part or model number, or empty."},
        "brand": {"type": "string", "description": "Make or manufacturer the customer names, or empty."},
        "quantity": {"type": "number", "description": "As required. 1 if no quantity is given."},
        "unit": {"type": "string", "description": "each, set, metre, lot..., or empty."},
        "specification": {
            "type": "string",
            "description": "Technical requirements for this line in one or two sentences, or empty.",
        },
        "source_document": {"type": "string", "description": "The file name it was read from."},
    },
    "required": ["description", "part_number", "brand", "quantity", "unit", "specification", "source_document"],
    "additionalProperties": False,
}

SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Two or three sentences: what the customer wants, for what, and by when.",
        },
        "customer": {"type": "string", "description": "The buying organisation or end user, or empty."},
        "deadline": {"type": "string", "description": "Bid closing date as YYYY-MM-DD, or empty."},
        "conditions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Requirements that are not line items: certificates, country of origin, "
            "delivery, warranty, installation, standards, payment, penalties.",
        },
        "missing": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What the documents do not say that a supplier would need to price it.",
        },
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file_name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["requirement", "supplier_quote", "other"]},
                },
                "required": ["file_name", "kind"],
                "additionalProperties": False,
            },
            "description": "Every document given, and what it is.",
        },
        "items": {"type": "array", "items": _ITEM},
    },
    "required": ["summary", "customer", "deadline", "conditions", "missing", "documents", "items"],
    "additionalProperties": False,
}

INSTRUCTIONS: Final = """\
You read the documents of a customer's enquiry for Hamdaz, a trading and \
supply company in the UAE that sources products for oil & gas, utilities and \
government buyers.

List every distinct item the customer asks to be supplied, once. When the \
same item appears in a bill of quantities and in a specification, give one \
line that combines them. Keep part numbers and brands exactly as written. Do \
not invent quantities, part numbers or brands; leave them empty.

Some documents may be a supplier's quotation or price offer sent to Hamdaz. \
Mark those as supplier_quote and take NO items from them — their lines are \
offers, not what the customer needs. Cover letters, terms and forms are \
"other".

Conditions are the requirements that are not items. Missing is what a \
supplier would ask before quoting."""


# ── what is sent ───────────────────────────────────────────────────────


@dataclass(slots=True)
class Prepared:
    """The message parts for one call, and what was left out of it."""

    parts: list[dict[str, Any]] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)


def trim(text: str, budget: int) -> str:
    """Head and tail of ``text`` within ``budget`` characters."""
    if len(text) <= budget:
        return text
    head = int(budget * 0.7)
    tail = budget - head
    return f"{text[:head]}\n[… {len(text) - budget} characters left out …]\n{text[-tail:]}"


def parts_for(readables: list[tuple[Readable, str]], *, context: str) -> Prepared:
    """The user message for these documents, each labelled with where it came from.

    ``readables`` pairs a prepared document with a short label of its origin
    ("task attachment", "task folder: RFQ/…"). ``context`` is what we know of
    the task, so the model can tell the customer from the supplier.
    """
    out = Prepared()
    out.parts.append({"type": "text", "text": context})
    texts = [r for r, _ in readables if r.kind == "text"]
    share = max(8_000, min(DOC_TEXT_BUDGET, TOTAL_TEXT_BUDGET // max(1, len(texts))))
    file_bytes = 0
    for readable, origin in readables:
        label = f"=== {readable.file_name} ({origin}) ==="
        if readable.kind == "text":
            out.parts.append({"type": "text", "text": f"{label}\n{trim(readable.text or '', share)}"})
            out.names.append(readable.file_name)
            continue
        data = readable.data or b""
        if file_bytes + len(data) > TOTAL_FILE_BYTES:
            out.left_out.append(f"{readable.file_name}: a scan too large to send with the rest")
            continue
        file_bytes += len(data)
        # Claude reads a PDF page by page, scans included, and an image as it is.
        source = {
            "type": "base64",
            "media_type": readable.media_type,
            "data": base64.standard_b64encode(data).decode(),
        }
        out.parts.append({"type": "text", "text": label})
        if readable.kind == "image":
            out.parts.append({"type": "image", "source": source})
        else:
            out.parts.append({"type": "document", "source": source, "title": readable.file_name[:200]})
        out.names.append(readable.file_name)
    return out


# ── what comes back ────────────────────────────────────────────────────


@dataclass(slots=True)
class Requirement:
    description: str
    part_number: str | None
    brand: str | None
    quantity: Decimal | None
    unit: str | None
    specification: str | None
    source_document: str | None


@dataclass(slots=True)
class Reading:
    summary: str = ""
    customer: str = ""
    deadline: str = ""
    conditions: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    kinds: dict[str, str] = field(default_factory=dict)
    items: list[Requirement] = field(default_factory=list)


def _s(value: Any, limit: int | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:limit].rstrip() if limit else text


def _qty(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number.quantize(Decimal("0.0001"))


def parse(raw: str) -> Reading:
    """The model's answer, checked. Anything malformed is dropped, not guessed at."""
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError("The model did not answer in the expected shape.") from exc
    if not isinstance(payload, dict):
        raise ValueError("The model did not answer in the expected shape.")
    out = Reading(
        summary=str(payload.get("summary") or "").strip(),
        customer=str(payload.get("customer") or "").strip()[:300],
        deadline=str(payload.get("deadline") or "").strip()[:40],
        conditions=[str(c).strip() for c in payload.get("conditions") or [] if str(c).strip()],
        missing=[str(m).strip() for m in payload.get("missing") or [] if str(m).strip()],
    )
    for doc in payload.get("documents") or []:
        if isinstance(doc, dict) and doc.get("file_name"):
            out.kinds[str(doc["file_name"])] = str(doc.get("kind") or "other")
    seen: set[tuple[str, str]] = set()
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        description = _s(item.get("description"))
        if not description:
            continue
        key = (description.casefold(), (_s(item.get("part_number")) or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        out.items.append(
            Requirement(
                description=description,
                part_number=_s(item.get("part_number"), 120),
                brand=_s(item.get("brand"), 120),
                quantity=_qty(item.get("quantity")),
                unit=_s(item.get("unit"), 40),
                specification=_s(item.get("specification")),
                source_document=_s(item.get("source_document"), 255),
            )
        )
    return out
