"""Have we met this item before? Where, from whom, and at what rate.

No model is asked. A requirement line is compared with three records of what
this company has already handled:

* **supplier quote lines** — what suppliers offered us, at what price;
* **quote request lines** — what we quoted customers, at what price and cost;
* **Zoho Books** — the items catalogue (its sale and purchase rates), and the
  purchase orders, bills and quotes that name a matching item.

Matching is by part number first (the same code, once punctuation and case are
gone) and then by the words of the description, weighted towards how much of
*the requirement* the candidate covers — a past line that says more than the
requirement is still the same thing; one that says less may not be.

Lines from the enquiry's own supplier quotes are not history: they are what is
on the table now, and they are shown as suppliers of this enquiry instead.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comparison import SupplierQuote, SupplierQuoteItem
from app.models.quoting import QuoteRequest, QuoteRequestItem

logger = logging.getLogger("hamdaz.enquiries")

#: Below this a candidate is not the same item.
THRESHOLD: Final = 0.55
#: History rows kept per line, best first.
KEEP: Final = 8
#: Past rows read from each table, newest first.
POOL_LIMIT: Final = 20_000
#: Lines whose Zoho documents are looked up — three calls each.
ZOHO_LINES: Final = 15

_STOP: Final = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or", "per", "the", "to", "with", "without", "supply", "supplying", "supplied", "provide", "providing", "delivery", "deliver", "item", "items", "qty", "quantity", "nos", "no", "each", "unit", "units", "pcs", "pc", "set", "sets", "lot", "new", "required", "requirement", "complete", "type", "model", "make", "brand", "similar", "equivalent", "approved"]
)
_WORD = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*")


def norm_pn(value: str | None) -> str:
    """A part number without punctuation, spacing or case."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def tokens(*texts: str | None) -> frozenset[str]:
    """The words that identify an item: no filler, no single letters."""
    words: set[str] = set()
    for text in texts:
        for word in _WORD.findall((text or "").lower()):
            if word in _STOP or (len(word) < 2 and not word.isdigit()):
                continue
            words.add(word)
            # "dn-50" and "dn50" are the same word to a buyer.
            if any(c in word for c in "./-"):
                words.add(re.sub(r"[./-]", "", word))
    return frozenset(words)


def score(
    want_pn: str, want_tokens: frozenset[str], want_brand: str | None,
    have_pn: str, have_tokens: frozenset[str], have_brand: str | None,
) -> float:
    """How surely two descriptions are the same item, 0 to 1."""
    if len(want_pn) >= 4 and want_pn == have_pn:
        return 1.0
    if len(want_pn) >= 6 and len(have_pn) >= 6 and (want_pn in have_pn or have_pn in want_pn):
        return 0.9
    if not want_tokens or not have_tokens:
        return 0.0
    shared = want_tokens & have_tokens
    if len(shared) < 2:
        return 0.0
    coverage = len(shared) / len(want_tokens)
    jaccard = len(shared) / len(want_tokens | have_tokens)
    value = 0.65 * coverage + 0.35 * jaccard
    # A part number on both that differs is a strong "not the same".
    if len(want_pn) >= 4 and len(have_pn) >= 4:
        value *= 0.6
    a, b = norm_pn(want_brand), norm_pn(have_brand)
    if a and b and a not in b and b not in a:
        value *= 0.7
    return round(value, 3)


# ── what we have met ───────────────────────────────────────────────────


@dataclass(slots=True)
class Known:
    """One line from our history."""

    source: str  # supplier_quote | quote_request | zoho_item
    ref: str
    when: date | None
    counterparty: str | None
    description: str
    part_number: str | None
    brand: str | None
    rate: Decimal | None
    currency: str | None
    quantity: Decimal | None = None
    cost_rate: Decimal | None = None
    supplier: str | None = None
    zoho_item_id: str | None = None
    #: Lines of this enquiry's own supplier quotes: offers now, not history.
    current: bool = False
    pn: str = ""
    words: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self.pn = norm_pn(self.part_number)
        self.words = tokens(self.description, self.part_number, self.brand)


class Pool:
    """Everything known, with a word index so a line is not compared with all of it."""

    def __init__(self, rows: list[Known]) -> None:
        self.rows = rows
        self._by_word: dict[str, list[int]] = defaultdict(list)
        self._by_pn: dict[str, list[int]] = defaultdict(list)
        for i, row in enumerate(rows):
            for word in row.words:
                self._by_word[word].append(i)
            if len(row.pn) >= 4:
                self._by_pn[row.pn].append(i)

    def candidates(self, pn: str, words: frozenset[str]) -> set[int]:
        found = set(self._by_pn.get(pn, ())) if len(pn) >= 4 else set()
        # The rarer words narrow it; "cable" alone would bring back half the pool.
        ranked = sorted(words, key=lambda w: len(self._by_word.get(w, ())))
        for word in ranked[:6]:
            hits = self._by_word.get(word, ())
            if len(hits) <= 2_000:
                found.update(hits)
        return found

    def match(
        self, description: str, part_number: str | None, brand: str | None
    ) -> list[tuple[float, Known]]:
        pn = norm_pn(part_number)
        words = tokens(description, part_number, brand)
        scored: list[tuple[float, Known]] = []
        for i in self.candidates(pn, words):
            row = self.rows[i]
            value = score(pn, words, brand, row.pn, row.words, row.brand)
            if value >= THRESHOLD:
                scored.append((value, row))
        scored.sort(key=lambda p: (p[0], p[1].when or date.min), reverse=True)
        return scored


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


async def load_pool(
    session: AsyncSession, *, task_id: str, comparison_id: uuid.UUID | None
) -> list[Known]:
    """Our supplier quote lines and quote request lines, newest first."""
    rows: list[Known] = []

    quoted = await session.execute(
        select(
            SupplierQuoteItem.description,
            SupplierQuoteItem.part_number,
            SupplierQuoteItem.brand,
            SupplierQuoteItem.unit_price,
            SupplierQuoteItem.quantity,
            SupplierQuote.supplier_name,
            SupplierQuote.currency,
            SupplierQuote.quote_number,
            SupplierQuote.created_at,
            SupplierQuote.comparison_id,
        )
        .join(SupplierQuote, SupplierQuoteItem.quote_id == SupplierQuote.id)
        .order_by(SupplierQuote.created_at.desc())
        .limit(POOL_LIMIT)
    )
    for r in quoted.all():
        rows.append(
            Known(
                source="supplier_quote",
                ref=r.quote_number or r.supplier_name,
                when=_day(r.created_at),
                counterparty=r.supplier_name,
                description=r.description,
                part_number=r.part_number,
                brand=r.brand,
                rate=r.unit_price if r.unit_price else None,
                currency=r.currency,
                quantity=r.quantity,
                supplier=r.supplier_name,
                current=comparison_id is not None and r.comparison_id == comparison_id,
            )
        )

    requested = await session.execute(
        select(
            QuoteRequestItem.name,
            QuoteRequestItem.description,
            QuoteRequestItem.item_code,
            QuoteRequestItem.brand,
            QuoteRequestItem.rate,
            QuoteRequestItem.cost_rate,
            QuoteRequestItem.quantity,
            QuoteRequest.reference,
            QuoteRequest.title,
            QuoteRequest.customer_name,
            QuoteRequest.currency,
            QuoteRequest.supplier_name,
            QuoteRequest.created_at,
            QuoteRequest.source_task_id,
        )
        .join(QuoteRequest, QuoteRequestItem.request_id == QuoteRequest.id)
        .order_by(QuoteRequest.created_at.desc())
        .limit(POOL_LIMIT)
    )
    for r in requested.all():
        if r.source_task_id == task_id:
            continue  # this very enquiry's own quote is not its history
        text = r.name if not r.description else f"{r.name} {r.description}"
        rows.append(
            Known(
                source="quote_request",
                ref=r.reference or r.title,
                when=_day(r.created_at),
                counterparty=r.customer_name,
                description=text,
                part_number=r.item_code,
                brand=r.brand,
                rate=r.rate if r.rate else None,
                currency=r.currency,
                quantity=r.quantity,
                cost_rate=r.cost_rate,
                supplier=r.supplier_name,
            )
        )
    return rows


# ── Zoho ───────────────────────────────────────────────────────────────

#: The items catalogue, swept once and kept: it is the same for every enquiry
#: and a sweep is a few dozen calls against a daily allowance.
_ZOHO_ITEMS: dict[str, Any] = {"at": 0.0, "rows": []}
_ZOHO_TTL: Final = 6 * 3600


async def zoho_items(zoho: Any) -> list[Known]:
    """Zoho's items catalogue as history rows: sale rate, purchase rate."""
    from app.zoho.catalogue import BY_KEY

    if time.monotonic() - _ZOHO_ITEMS["at"] > _ZOHO_TTL or not _ZOHO_ITEMS["rows"]:
        raw = await zoho.list_rows(BY_KEY["items"], limit=10_000)
        _ZOHO_ITEMS["rows"] = raw
        _ZOHO_ITEMS["at"] = time.monotonic()
    out: list[Known] = []
    for item in _ZOHO_ITEMS["rows"]:
        name = str(item.get("name") or item.get("item_name") or "").strip()
        if not name:
            continue
        description = " ".join(
            s for s in (name, item.get("description"), item.get("purchase_description")) if s
        )
        out.append(
            Known(
                source="zoho_item",
                ref=name,
                when=_day(item.get("last_modified_time") or item.get("created_time")),
                counterparty=None,
                description=description,
                part_number=item.get("sku") or item.get("part_number") or None,
                brand=item.get("brand") or item.get("manufacturer") or None,
                rate=_dec(item.get("rate")),
                currency=None,
                cost_rate=_dec(item.get("purchase_rate")),
                zoho_item_id=str(item.get("item_id") or "") or None,
            )
        )
    return out


def _dec(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    return number if number.is_finite() and number != 0 else None


#: Which Zoho documents name an item, and how each is described.
_ZOHO_DOCS: Final = (
    ("purchaseorders", "zoho_po", "purchaseorder_number", "vendor_name"),
    ("bills", "zoho_bill", "bill_number", "vendor_name"),
    ("estimates", "zoho_estimate", "estimate_number", "customer_name"),
)


async def zoho_documents(zoho: Any, item_id: str) -> list[dict[str, Any]]:
    """The latest purchase orders, bills and quotes that carry a Zoho item."""
    from app.zoho.catalogue import BY_KEY

    out: list[dict[str, Any]] = []
    for key, source, number_field, party_field in _ZOHO_DOCS:
        rows = await zoho.list_rows(
            BY_KEY[key], params={"item_id": item_id, "sort_column": "date", "sort_order": "D"}, limit=3
        )
        for row in rows[:3]:
            out.append(
                {
                    "source": source,
                    "ref": row.get(number_field) or "",
                    "date": row.get("date"),
                    "counterparty": row.get(party_field),
                    "rate": None,
                    "currency": row.get("currency_code"),
                    "total": row.get("total"),
                    "status": row.get("status"),
                }
            )
    return out


# ── putting it on the line ─────────────────────────────────────────────


def history_entry(value: float, row: Known) -> dict[str, Any]:
    return {
        "source": row.source,
        "ref": row.ref,
        "date": row.when.isoformat() if row.when else None,
        "counterparty": row.counterparty,
        "supplier": row.supplier,
        "description": row.description[:300],
        "part_number": row.part_number,
        "rate": str(row.rate) if row.rate is not None else None,
        "cost_rate": str(row.cost_rate) if row.cost_rate is not None else None,
        "currency": row.currency,
        "quantity": str(row.quantity) if row.quantity is not None else None,
        "score": value,
        "zoho_item_id": row.zoho_item_id,
    }


def status_of(history: list[dict[str, Any]], *, recent_months: int, today: date | None = None) -> str:
    """recent, history or new — by the latest date the item was met."""
    if not history:
        return "new"
    today = today or datetime.now(UTC).date()
    cutoff = today - timedelta(days=round(recent_months * 30.44))
    dates = [_day(h.get("date")) for h in history]
    latest = max((d for d in dates if d is not None), default=None)
    if latest is not None and latest >= cutoff:
        return "recent"
    return "history"


def suppliers_from(history: list[dict[str, Any]], current: list[tuple[float, Known]]) -> list[dict[str, Any]]:
    """Who supplied or offered it: one entry per supplier, latest first."""
    found: dict[str, dict[str, Any]] = {}

    def add(name: str | None, source: str, rate: Any, currency: Any, when: Any) -> None:
        if not name or not name.strip():
            return
        key = name.strip().casefold()
        entry = found.get(key)
        if entry is None or (when or "") > (entry.get("last_date") or ""):
            found[key] = {
                "name": name.strip(),
                "source": source,
                "role": None,
                "website": None,
                "email": None,
                "phone": None,
                "country": None,
                "evidence": None,
                "last_rate": rate,
                "currency": currency,
                "last_date": when,
                # The supplier library will say; until it exists nobody has.
                "partner": None,
            }

    for _value, row in current:
        add(row.supplier, "this_enquiry", str(row.rate) if row.rate else None, row.currency,
            row.when.isoformat() if row.when else None)
    for h in history:
        if h["source"] == "supplier_quote":
            add(h.get("supplier"), "supplier_quote", h.get("rate"), h.get("currency"), h.get("date"))
        elif h["source"] == "quote_request":
            add(h.get("supplier"), "quote_request", h.get("cost_rate"), h.get("currency"), h.get("date"))
        elif h["source"] in ("zoho_po", "zoho_bill"):
            add(h.get("counterparty"), h["source"], None, h.get("currency"), h.get("date"))
    return sorted(found.values(), key=lambda e: e.get("last_date") or "", reverse=True)
