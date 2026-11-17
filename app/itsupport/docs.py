"""The two things IT Support does with documents: read what a customer asks
for, and read and compare what suppliers offer.

Both run the ERP's own readers, so the chat gets the same answer the app
would:

* **Requirements**: the enquiry analysis's reader (``app.enquiries.reading``,
  through ``ClaudeEnquiry.read_documents``): every file in one Claude call,
  items with quantities and specs, the conditions, the deadline, what's
  missing.
* **Quotes**: the comparison's reader (``app.comparison``), which needs no
  model for a quote with a text layer, then its matching and analysis when
  two or more quotes share a currency: cheapest supplier, the split award,
  lines only one supplier bid on, lines missing per supplier.

**First cut, read only** (2026-11-13): nothing is filed to the task's folder
and no quote is raised in the app; the reply is the reading. Putting the
quotes into the costing sheet is the next step, after a "yes" in the chat.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from app.comparison import service as comparison
from app.comparison.extraction import ExtractionError, NoPricesError, QuoteExtractor
from app.comparison.schemas import QuoteIn
from app.enquiries import reading
from app.enquiries.claude import ClaudeEnquiry
from app.followups import service as fu
from app.itsupport import distributors
from app.itsupport.files import Opened
from app.itsupport.work import UAE, both
from app.proposals.sharepoint import ProposalTask

logger = logging.getLogger("hamdaz.itsupport")

#: Lines shown in the chat; the rest are counted.
SHOW_ITEMS: Final = 15
SHOW_CONDITIONS: Final = 6


def _e(value: Any) -> str:
    return html.escape(str(value or ""))


def _qty(value: Decimal | float | None) -> str:
    if value is None:
        return ""
    number = Decimal(str(value))
    return f"{number.normalize():f}" if number == number.to_integral() else f"{number:f}"


def _bcd(task: ProposalTask | None) -> str:
    """The task's BCD as the team means it (UAE, with IST), never the raw
    stored value, which SharePoint keeps on the site's Pacific clock."""
    due = fu.due_of(task) if task else None
    return f"{due.astimezone(UAE):%a %d %b %Y} {both(due)}" if due else "not given"


def _clean(value: str | None) -> str:
    """A read field, without the next cells a spreadsheet row ran into it
    ("30 days | Delivery: 3-4 weeks |")."""
    return (value or "").split(" | ")[0].strip(" |")


def _money(value: float | Decimal | None, currency: str) -> str:
    return "" if value is None else f"{currency} {Decimal(str(value)):,.2f}"


# ── requirements ───────────────────────────────────────────────────────


async def read_requirements(
    claude: ClaudeEnquiry, opened: list[Opened], task: ProposalTask | None, user_key: str,
) -> tuple[reading.Reading, list[str]]:
    """Every readable file in one call; what was left out, in words."""
    context = (
        f"Enquiry: {task.title if task else 'not known yet, sent in a Teams chat'}\n"
        f"End user: {(task.end_user if task else '') or 'not given'}\n"
        f"Bid closing date: {_bcd(task)}"
    )
    prepared = reading.parts_for(
        [(o.readable, "sent in Teams") for o in opened if o.readable is not None],
        context=context,
    )
    raw, _, _ = await claude.read_documents(
        instructions=reading.INSTRUCTIONS, content=prepared.parts, schema=reading.SCHEMA,
        user_key=user_key,
    )
    return reading.parse(raw), prepared.left_out


def requirements_html(result: reading.Reading, left_out: list[str], *, offer_find: bool) -> str:
    parts = []
    if result.summary:
        parts.append(_e(result.summary))
    facts = [f"<b>Customer:</b> {_e(result.customer)}" if result.customer else "",
             f"<b>Deadline:</b> {_e(result.deadline)}" if result.deadline else ""]
    if any(facts):
        parts.append(" · ".join(f for f in facts if f))
    if result.items:
        rows = []
        for item in result.items[:SHOW_ITEMS]:
            qty = " ".join(x for x in (_qty(item.quantity), item.unit or "") if x)
            maker = " ".join(x for x in (item.brand or "", item.part_number or "") if x)
            line = f"{_e(qty)} × {_e(item.description)}" if qty else _e(item.description)
            if maker:
                line += f" <i>({_e(maker)})</i>"
            rows.append(f"<li>{line}</li>")
        more = len(result.items) - SHOW_ITEMS
        tail = f"<br><i>…and {more} more</i>" if more > 0 else ""
        parts.append(f"<b>{len(result.items)} item(s):</b><ol>{''.join(rows)}</ol>{tail}")
    else:
        parts.append("I couldn't find an item list in it.")
    if result.conditions:
        conditions = "".join(f"<li>{_e(c)}</li>" for c in result.conditions[:SHOW_CONDITIONS])
        parts.append(f"<b>Conditions:</b><ul>{conditions}</ul>")
    if result.missing:
        parts.append("<b>Not stated:</b> " + _e("; ".join(result.missing[:4])))
    if left_out:
        parts.append("<i>Left out: " + _e("; ".join(left_out)) + "</i>")
    if offer_find and result.items:
        parts.append("Want me to find suppliers for these?")
    return "<br><br>".join(parts)


def as_find_ask(result: reading.Reading, place: str = "the UAE") -> distributors.Ask:
    """The read items, as the distributor finder takes them."""
    items = tuple(
        distributors.Item(
            description=(item.description or "")[:200], brand=item.brand or "",
            part=item.part_number or "", qty=_qty(item.quantity),
            details=(item.specification or "")[:300],
        )
        for item in result.items[:10]
    )
    return distributors.Ask(items, place)


# ── supplier quotes ────────────────────────────────────────────────────


@dataclass(slots=True)
class QuotesRead:
    quotes: list[QuoteIn] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: Customer documents sent with the quotes, kept out of the comparison.
    skipped: list[str] = field(default_factory=list)
    analysis: dict[str, Any] | None = None


async def read_quotes(extractor: QuoteExtractor, opened: list[Opened]) -> QuotesRead:
    out = QuotesRead()
    for o in opened:
        if o.looks_like == "requirement":
            out.skipped.append(o.name)
            continue
        if o.readable is None:
            out.problems.append(f"{o.name}: {o.problem}")
            continue
        try:
            result = await extractor.read(o.readable)
        except ExtractionError as exc:
            # The reader's own words are for the app's upload screen.
            out.problems.append(f"{o.name}: no prices I could read" if isinstance(
                exc, NoPricesError) else f"{o.name}: {exc}")
            continue
        out.quotes.append(comparison.quote_in_from(result, o.name))
        out.names.append(o.name)
    currencies = {q.currency for q in out.quotes}
    if len(out.quotes) >= 2 and len(currencies) == 1:
        out.analysis = await comparison.compare(
            extractor, out.quotes, currency=currencies.pop()
        )
    return out


def quotes_html(read: QuotesRead, task: ProposalTask | None) -> str:
    if not read.quotes:
        why = "; ".join(read.problems) or "no prices found"
        them = "them" if len(read.problems) > 1 else "it"
        return f"I couldn't read prices from {them}: {_e(why)}"
    parts = []
    rows = []
    for q in read.quotes:
        total = q.quoted_total if q.quoted_total is not None else sum(
            (comparison.line_total(i) for i in q.items), Decimal(0)
        )
        bits = [f"{len(q.items)} line(s)", _money(total, q.currency)]
        bits += [f"valid {_clean(q.validity)}" if _clean(q.validity) else "",
                 f"delivery {_clean(q.delivery_time)}" if _clean(q.delivery_time) else "",
                 _clean(q.incoterms)]
        said = " · ".join(b for b in bits if b)
        rows.append(f"<li><b>{_e(q.supplier_name)}</b> — {_e(said)}</li>")
    parts.append(f"<b>Read {len(read.quotes)} quote(s):</b><ul>{''.join(rows)}</ul>")
    a = read.analysis
    if a:
        currency = a["currency"]
        best = a.get("cheapest_supplier")
        if best:
            note = "" if a.get("all_suppliers_complete") else " (of those who quoted every line)"
            parts.append(f"<b>Cheapest overall{note}:</b> {_e(best['supplier_name'])}, "
                         f"{_e(_money(best['total'], currency))}")
        split = a.get("split_award") or {}
        if (saving := split.get("saving")) and saving.get("amount"):
            saved = _money(saving["amount"], currency)
            parts.append(f"Picking the cheapest per line saves {_e(saved)}"
                         f" against {_e(saving['against'])}.")
        missing = [f"{s['supplier_name']} misses {len(s['missing_items'])}"
                   for s in a["suppliers"] if s.get("missing_items")]
        if missing:
            parts.append("<b>Gaps:</b> " + _e("; ".join(missing)))
        single = [g["label"] for g in a["groups"] if g.get("single_source")]
        if single and len(read.quotes) > 1:
            parts.append(f"<b>Only one supplier quoted:</b> {_e('; '.join(single[:5]))}"
                         + (f" (+{len(single) - 5})" if len(single) > 5 else ""))
    elif len(read.quotes) > 1:
        currencies = ", ".join(sorted({q.currency for q in read.quotes}))
        parts.append(f"They're in different currencies ({_e(currencies)}), so I haven't "
                     "compared them line by line; the costing sheet converts at Zoho's rate.")
    if read.problems:
        parts.append("<i>Couldn't read: " + _e("; ".join(read.problems)) + "</i>")
    if read.skipped:
        parts.append("<i>" + _e(", ".join(read.skipped)) + " looks like the customer's "
                     "request, so I kept it out of the comparison.</i>")
    where = f" for {_e((task.title or '')[:60])}" if task else ""
    parts.append(f"Putting these into the costing sheet{where} is the next thing I'm "
                 "learning; for now they go in on the quote's page in the ERP.")
    return "<br><br>".join(parts)
