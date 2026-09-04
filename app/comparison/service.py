"""Building, analysing and storing quote comparisons.

The important function here is ``to_domain``: it turns a posted quote — whether
it came from an uploaded document or from someone typing into a form — into the
normalised shape the analysis works on. Once past it the two paths are identical,
which is why the manual route needs no analysis code of its own and cannot drift
away from the uploaded one.

It is also where currency conversion and the missing-line-total rule are applied,
so both happen exactly once and in one place.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.comparison import analysis as analysis_mod
from app.comparison.analysis import Offer, Quote
from app.comparison.extraction import QuoteExtractor
from app.comparison.schemas import ComparisonIn, ItemIn, QuoteIn
from app.models.comparison import (
    ComparisonStatus,
    QuoteComparison,
    SupplierQuote,
    SupplierQuoteItem,
)
from app.models.user import User

logger = logging.getLogger("hamdaz.comparison")


class ComparisonError(Exception):
    """A comparison operation was refused. Safe to show a user."""


class ComparisonNotFoundError(ComparisonError):
    pass


def line_total(item: ItemIn) -> Decimal:
    """What this line costs.

    The printed total wins when the supplier gave one, even where it disagrees
    with quantity x unit price — that disagreement is reported as a finding
    rather than quietly corrected here. Only its absence is computed.
    """
    if item.line_total is not None:
        return item.line_total
    return (item.quantity or Decimal(0)) * (item.unit_price or Decimal(0))


def to_domain(quotes: list[QuoteIn], *, ids: list[str] | None = None) -> list[Quote]:
    """Posted quotes as the analysis sees them: one currency, totals resolved.

    ``ids`` lets a saved comparison reuse its real database ids, so a stored
    analysis still points at rows that exist. Without it, ids are positional and
    only meaningful within one unsaved response.
    """
    out: list[Quote] = []
    for index, incoming in enumerate(quotes):
        quote_id = (ids[index] if ids and index < len(ids) else f"q{index}")
        rate = incoming.fx_rate or Decimal(1)

        offers = [
            Offer(
                quote_id=quote_id,
                supplier_name=incoming.supplier_name,
                item_id=f"{quote_id}-i{position}",
                description=item.description,
                part_number=item.part_number,
                quantity=item.quantity or Decimal(0),
                # Converted here, once. Everything downstream is already in the
                # comparison's currency and never has to ask again.
                unit_price=(item.unit_price or Decimal(0)) * rate,
                line_total=line_total(item) * rate,
                lead_time=item.lead_time,
            )
            for position, item in enumerate(incoming.items)
        ]

        out.append(
            Quote(
                quote_id=quote_id,
                supplier_name=incoming.supplier_name,
                currency=incoming.currency,
                fx_rate=rate,
                items=offers,
                discount=incoming.discount * rate if incoming.discount is not None else None,
                freight=incoming.freight * rate if incoming.freight is not None else None,
                tax=incoming.tax * rate if incoming.tax is not None else None,
                quoted_total=(
                    incoming.quoted_total * rate if incoming.quoted_total is not None else None
                ),
                delivery_time=incoming.delivery_time,
                payment_terms=incoming.payment_terms,
                validity=incoming.validity,
                warranty=incoming.warranty,
                incoterms=incoming.incoterms,
                extraction_note=incoming.extraction_note,
            )
        )
    return out


async def compare(
    extractor: QuoteExtractor,
    quotes: list[QuoteIn],
    *,
    currency: str = "AED",
    ids: list[str] | None = None,
) -> dict[str, Any]:
    """Match the line items, then compute the comparison."""
    if not quotes:
        raise ComparisonError("A comparison needs at least one supplier quote")

    domain = to_domain(quotes, ids=ids)
    groups = await analysis_mod.match_items(extractor, domain)
    return analysis_mod.analyse(domain, groups, currency=currency)


# ── storage ────────────────────────────────────────────────────────────


def _row(incoming: QuoteIn, documents: dict[str, tuple[str, bytes]] | None = None) -> SupplierQuote:
    stored = (documents or {}).get(incoming.file_name or "")
    quote = SupplierQuote(
        supplier_name=incoming.supplier_name,
        quote_number=incoming.quote_number,
        quote_date=incoming.quote_date,
        currency=incoming.currency,
        fx_rate=incoming.fx_rate,
        validity=incoming.validity,
        delivery_time=incoming.delivery_time,
        payment_terms=incoming.payment_terms,
        warranty=incoming.warranty,
        incoterms=incoming.incoterms,
        contact=incoming.contact,
        notes=incoming.notes,
        discount=incoming.discount,
        freight=incoming.freight,
        tax=incoming.tax,
        quoted_total=incoming.quoted_total,
        source=incoming.source,
        file_name=incoming.file_name,
        file_type=stored[0] if stored else None,
        file_bytes=stored[1] if stored else None,
        extraction_note=incoming.extraction_note,
    )
    quote.items = [
        SupplierQuoteItem(
            position=position,
            description=item.description,
            part_number=item.part_number,
            brand=item.brand,
            unit=item.unit,
            quantity=item.quantity,
            unit_price=item.unit_price,
            line_total=item.line_total,
            lead_time=item.lead_time,
        )
        for position, item in enumerate(incoming.items)
    ]
    return quote


async def save(
    session: AsyncSession,
    extractor: QuoteExtractor,
    payload: ComparisonIn,
    *,
    author: User,
    documents: dict[str, tuple[str, bytes]] | None = None,
) -> QuoteComparison:
    """Store a comparison and the analysis that goes with it.

    The analysis is computed here rather than taken from the request. A caller
    could otherwise post whatever totals they liked alongside the line items, and
    the saved record would no longer follow from its own inputs.
    """
    if not payload.quotes:
        raise ComparisonError("A comparison needs at least one supplier quote")

    comparison = QuoteComparison(
        title=payload.title.strip(),
        reference=payload.reference,
        notes=payload.notes,
        currency=payload.currency.upper(),
        status=ComparisonStatus.SAVED,
        # The object, not the id: an unloaded ``created_by`` is a lazy SELECT
        # when the response names its author.
        created_by=author,
    )
    comparison.quotes = [_row(q, documents) for q in payload.quotes]
    session.add(comparison)
    # Ids exist only after the flush, and the analysis refers to them.
    await session.flush()

    result = await compare(
        extractor,
        payload.quotes,
        currency=comparison.currency,
        ids=[str(q.id) for q in comparison.quotes],
    )
    comparison.analysis = result
    # A Python value, not func.now(): a SQL expression leaves the attribute
    # expired after the flush, and reading it back to build the response would
    # then need IO from inside serialisation.
    comparison.analysed_at = datetime.now(UTC)
    await session.flush()
    # created_at / updated_at are server-generated, so they are not loaded until
    # they are fetched. Doing it here keeps the read inside the async session.
    await session.refresh(comparison)
    return comparison


async def get(session: AsyncSession, comparison_id: uuid.UUID) -> QuoteComparison:
    comparison = await session.get(QuoteComparison, comparison_id)
    if comparison is None:
        raise ComparisonNotFoundError("No such comparison")
    return comparison


async def for_user(
    session: AsyncSession, user_id: uuid.UUID | None = None, *, limit: int = 100
) -> list[QuoteComparison]:
    """Saved comparisons, newest first.

    ``user_id`` narrows to one person's own. Left out, it returns everyone's:
    the point of a comparison is that colleagues can see what was decided and
    on what basis.
    """
    query = (
        select(QuoteComparison)
        .order_by(QuoteComparison.created_at.desc())
        .limit(limit)
    )
    if user_id is not None:
        query = query.where(QuoteComparison.created_by_id == user_id)
    return list((await session.scalars(query)).all())


async def delete(
    session: AsyncSession, comparison: QuoteComparison, *, actor: User, is_admin: bool = False
) -> None:
    if comparison.created_by_id != actor.id and not is_admin:
        raise ComparisonError("Only the person who created a comparison can delete it")
    await session.delete(comparison)
    await session.flush()


def summarise(comparison: QuoteComparison) -> dict[str, Any]:
    """The headline numbers for a list row, read from the stored analysis."""
    stored = comparison.analysis or {}
    cheapest = stored.get("cheapest_supplier") or {}
    total = cheapest.get("total")
    return {
        "id": comparison.id,
        "title": comparison.title,
        "reference": comparison.reference,
        "currency": comparison.currency,
        "status": comparison.status,
        "created_by_name": comparison.created_by.display_name if comparison.created_by else None,
        "created_at": comparison.created_at,
        "supplier_count": len(comparison.quotes),
        "item_count": stored.get("item_count", 0),
        "best_total": Decimal(str(total)) if total is not None else None,
        "best_supplier": cheapest.get("supplier_name"),
    }
