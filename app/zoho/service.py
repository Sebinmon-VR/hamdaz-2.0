"""Resolving everything connected to one quote.

Most of it needs no extra call. Zoho embeds the sales orders, the attached
documents, the line items and the invoice ids directly in the estimate, so the
only genuine lookups are the customer, the catalogue records behind the line
items, and the comment history.

Two rules shape this module:

**Opt in, branch by branch.** A full fan-out is several upstream calls, and Zoho
allows 100 a minute for the whole organisation. Nothing is fetched that the
caller did not name in ``include``.

**A branch fails alone.** A deleted customer, or a scope that cannot read
invoices, must not blank the rest of the response — that turns one missing field
into a broken page. Each branch reports its own outcome and the others carry on.
The invoices branch is the live example: this org's token gets 403 on every
invoice read, so it reports the ids it can see and says plainly why it cannot
expand them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from app.zoho.client import ZohoBooks, ZohoError
from app.zoho.schemas import BranchOut, QuoteDetailOut, RelatedOut

logger = logging.getLogger("hamdaz.zoho")

#: Everything ``include`` accepts. Ordered as a person would read them.
BRANCHES: Final[tuple[str, ...]] = (
    "customer",
    "items",
    "salesorders",
    "invoices",
    "comments",
)

#: Line items repeat the same product across a quote; without this a ten-line
#: quote for one item would fetch it ten times.
_MAX_ITEMS: Final = 40


def parse_include(raw: str | None) -> list[str]:
    """``"customer,items"`` -> ``["customer", "items"]``.

    Unknown names are dropped rather than rejected: a client asking for something
    this version does not have should get the rest, not a 400.
    """
    if raw is None:
        return list(BRANCHES)
    if raw.strip().casefold() in ("all", "*"):
        return list(BRANCHES)
    wanted = [p.strip().casefold() for p in raw.split(",") if p.strip()]
    return [b for b in BRANCHES if b in wanted]


async def _branch(label: str, coro) -> BranchOut:
    """Run one lookup, and turn its failure into a reported outcome."""
    try:
        return BranchOut(ok=True, data=await coro)
    except ZohoError as exc:
        logger.info("zoho related branch %s failed: %s", label, exc)
        return BranchOut(ok=False, error=str(exc))


async def _customer(zoho: ZohoBooks, customer_id: str | None):
    if not customer_id:
        return None
    contact = await zoho.contact(customer_id)
    return {
        "id": contact.get("contact_id"),
        "name": contact.get("contact_name"),
        "company_name": contact.get("company_name"),
        "email": contact.get("email") or None,
        "phone": contact.get("phone") or contact.get("mobile") or None,
        "payment_terms": contact.get("payment_terms_label") or None,
        "currency_code": contact.get("currency_code"),
        "outstanding": contact.get("outstanding_receivable_amount"),
        "billing_address": contact.get("billing_address") or None,
        "contact_persons": [
            {
                "name": " ".join(
                    p for p in (cp.get("first_name"), cp.get("last_name")) if p
                ).strip()
                or None,
                "email": cp.get("email") or None,
                "phone": cp.get("phone") or cp.get("mobile") or None,
                "designation": cp.get("designation") or None,
            }
            for cp in contact.get("contact_persons") or []
        ],
    }


async def _items(zoho: ZohoBooks, detail: QuoteDetailOut):
    """The catalogue record behind each distinct line item."""
    seen: list[str] = []
    for line in detail.line_items:
        if line.item_id and line.item_id not in seen:
            seen.append(line.item_id)

    fetched = await asyncio.gather(
        *(zoho.item(i) for i in seen[:_MAX_ITEMS]), return_exceptions=True
    )

    out = []
    for item_id, result in zip(seen, fetched, strict=False):
        if isinstance(result, BaseException):
            # One unreadable item should not lose the rest of the catalogue.
            out.append({"id": item_id, "error": str(result)})
            continue
        out.append(
            {
                "id": result.get("item_id"),
                "name": result.get("name"),
                "sku": result.get("sku") or None,
                "description": result.get("description") or None,
                "rate": result.get("rate"),
                "unit": result.get("unit") or None,
                "status": result.get("status"),
                "stock_on_hand": result.get("stock_on_hand"),
            }
        )
    return out


async def _invoices(detail: QuoteDetailOut, zoho: ZohoBooks):
    """What we can say about invoices raised against this quote.

    The estimate itself carries the ids and the invoiced amount, and those are
    readable. Expanding an id into an invoice is a separate permission that this
    org's token does not hold — so rather than fail, report what is known and say
    what is missing.
    """
    known = {
        "invoice_ids": detail.invoice_ids,
        "invoiced_amount": detail.invoiced_amount,
        "uninvoiced_amount": detail.uninvoiced_amount,
        "invoices": [],
    }
    if not detail.invoice_ids:
        return known

    expanded = await asyncio.gather(
        *(zoho.invoice(i) for i in detail.invoice_ids), return_exceptions=True
    )
    failures = [r for r in expanded if isinstance(r, BaseException)]
    known["invoices"] = [
        {
            "id": r.get("invoice_id"),
            "number": r.get("invoice_number"),
            "date": r.get("date"),
            "due_date": r.get("due_date"),
            "status": r.get("status"),
            "total": r.get("total"),
            "balance": r.get("balance"),
        }
        for r in expanded
        if not isinstance(r, BaseException)
    ]
    if failures:
        known["unreadable"] = len(failures)
        known["reason"] = (
            "This Zoho token cannot read invoices — the ids and totals above come "
            "from the quote itself. Add ZohoBooks.invoices.READ to the refresh "
            "token's scope to expand them."
        )
    return known


async def related(
    zoho: ZohoBooks, detail: QuoteDetailOut, include: list[str]
) -> RelatedOut:
    """Fetch the requested branches together, keeping each one's outcome separate."""
    out = RelatedOut(quote_id=detail.id, quote_number=detail.number, included=include)

    jobs: dict[str, object] = {}
    if "customer" in include:
        jobs["customer"] = _branch("customer", _customer(zoho, detail.customer_id))
    if "items" in include:
        jobs["items"] = _branch("items", _items(zoho, detail))
    if "invoices" in include:
        jobs["invoices"] = _branch("invoices", _invoices(detail, zoho))
    if "comments" in include:
        jobs["comments"] = _branch("comments", zoho.comments(detail.id))

    # Independent lookups, so they go together rather than one after another.
    if jobs:
        results = await asyncio.gather(*jobs.values())
        for name, result in zip(jobs.keys(), results, strict=True):
            setattr(out, name, result)

    if "salesorders" in include:
        # Already in the detail payload — embedding it costs nothing, so there is
        # no call to make and no way for this branch to fail.
        out.salesorders = BranchOut(
            ok=True, data=[s.model_dump() for s in detail.salesorders]
        )

    return out
