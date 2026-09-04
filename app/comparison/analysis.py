"""Comparing the quotes once they have been read.

The work splits in two, and the split is the whole design:

**Matching is a language problem**, so Claude does it. "Filter element FX-200",
"FX200 Filter Elm." and "Element, filter, FX 200" are one item, and no amount of
string distance reliably says so while also keeping FX-200 apart from FX-200H.

**Everything numeric is a Python problem**, so Python does it. Totals, spreads,
the split-award figure and every saving in here are computed from the matched
groups in ``Decimal``. A model is never asked to add up, because a number a model
produced is a number nobody can check — and these numbers go to a supplier.

The insights are deliberately blunt about *incomparability*. The most expensive
mistake in a quote comparison is not picking the wrong supplier, it is comparing
two totals that were never for the same scope: supplier A looks 12% cheaper
because they quietly left two lines out. Coverage is checked before price, and a
supplier who did not quote everything is flagged rather than ranked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from pydantic import BaseModel, Field

from app.comparison.extraction import ExtractionError, QuoteExtractor

logger = logging.getLogger("hamdaz.comparison")

_CENTS: Final = Decimal("0.01")

#: A unit price this far above the cheapest offer for the same item is called
#: out. Usually a genuine premium; sometimes a pack-size or currency misreading,
#: which is exactly why it is worth a look.
_OUTLIER_RATIO: Final = Decimal("2.0")

#: Below this the spread on a line is noise rather than a finding.
_NOTABLE_SPREAD_PCT: Final = Decimal("15")

_MATCH_SYSTEM: Final = """\
You group line items from competing supplier quotations so a procurement team can \
compare like with like.

Put items in the same group only when a buyer would accept either one for the \
same requirement. Judge by what the item IS — part number, brand, specification, \
size — not by how the wording looks.

Be conservative. Two groups that should have been one is a visible, harmless \
result: the team sees both lines. One group that should have been two is a wrong \
price comparison and can lose real money. When two items are similar but you are \
not certain they are interchangeable, keep them apart.

Watch specifically for: part numbers differing by a suffix that denotes a real \
variant (FX-200 vs FX-200H); the same item quoted per-piece by one supplier and \
per-pack by another; and accessories or spares that merely mention the main item \
in their description.

Every item id you were given must appear in exactly one group. Give each group a \
short neutral label a buyer would recognise.
"""


class _Group(BaseModel):
    label: str = Field(description="Short neutral name for the item")
    item_ids: list[str] = Field(description="Ids of the line items that are this item")
    note: str | None = Field(
        default=None, description="Only if something about this grouping is uncertain"
    )


class _Grouping(BaseModel):
    groups: list[_Group]


@dataclass(slots=True)
class Offer:
    """One supplier's price for one matched item, in the comparison currency."""

    quote_id: str
    supplier_name: str
    item_id: str
    description: str
    part_number: str | None
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    lead_time: str | None


@dataclass(slots=True)
class Quote:
    """A supplier's offer, normalised for comparison.

    Built from either an extracted document or a hand-typed form — by this point
    the two are indistinguishable, which is what lets the manual path share every
    line of the analysis.
    """

    quote_id: str
    supplier_name: str
    currency: str
    fx_rate: Decimal
    items: list[Offer] = field(default_factory=list)
    discount: Decimal | None = None
    freight: Decimal | None = None
    tax: Decimal | None = None
    quoted_total: Decimal | None = None
    delivery_time: str | None = None
    payment_terms: str | None = None
    validity: str | None = None
    warranty: str | None = None
    incoterms: str | None = None
    extraction_note: str | None = None


def _money(value: Decimal | None) -> Decimal:
    return (value or Decimal(0)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _out(value: Decimal | None) -> float | None:
    """Money for the stored snapshot.

    Rounded to cents first, so the float is exact at the precision shown. The
    ``Numeric`` columns remain the authority; this blob is what the screen reads.
    """
    return None if value is None else float(_money(value))


# ── matching (the model's half) ────────────────────────────────────────


async def match_items(extractor: QuoteExtractor, quotes: list[Quote]) -> list[dict[str, Any]]:
    """Group equivalent line items across suppliers.

    Falls back to grouping by exact part number, then by normalised description,
    if Claude is unavailable. The fallback is worse — it will not see that
    "FX200 Filter Elm." is the same item — but a degraded comparison beats none,
    and the manual path must keep working without an API key.
    """
    catalogue = [
        {
            "id": offer.item_id,
            "supplier": quote.supplier_name,
            "description": offer.description,
            "part_number": offer.part_number,
            "quantity": str(offer.quantity),
        }
        for quote in quotes
        for offer in quote.items
    ]
    if not catalogue:
        return []

    if not extractor.configured:
        logger.info("no Claude key; matching line items by part number and description")
        return _fallback_groups(quotes)

    if _part_numbers_settle_it(quotes):
        # Every line carries a part number and they already line up across
        # suppliers. Exact identifiers are better evidence than any judgement a
        # model could add, so this saves a whole Opus call for nothing lost.
        logger.info("part numbers match across suppliers; skipping the matching call")
        return _fallback_groups(quotes)

    import json

    try:
        client = extractor._anthropic()  # noqa: SLF001 - same package, one client
        response = await client.messages.parse(
            model=extractor._settings.anthropic_model,  # noqa: SLF001
            max_tokens=16000,
            system=[
                {"type": "text", "text": _MATCH_SYSTEM, "cache_control": {"type": "ephemeral"}}
            ],
            # Deciding equivalence is the judgement call in this module, and the
            # one place where thinking earns its cost.
            output_config={"effort": "high"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Group these line items from competing quotations.\n\n"
                        + json.dumps(catalogue, indent=1)
                    ),
                }
            ],
            output_format=_Grouping,
        )
    except Exception as exc:  # noqa: BLE001 - any failure falls back, never fails the comparison
        logger.warning("item matching failed, falling back to part numbers: %s", exc)
        return _fallback_groups(quotes)

    grouping = response.parsed_output
    if grouping is None:
        return _fallback_groups(quotes)

    known = {offer.item_id for quote in quotes for offer in quote.items}
    groups: list[dict[str, Any]] = []
    placed: set[str] = set()
    for group in grouping.groups:
        ids = [i for i in group.item_ids if i in known and i not in placed]
        if not ids:
            continue
        placed.update(ids)
        groups.append({"label": group.label, "item_ids": ids, "note": group.note})

    # The prompt asks for every id exactly once; trusting that without checking
    # would silently drop a priced line from the comparison.
    for item_id in known - placed:
        offer = _find(quotes, item_id)
        if offer is not None:
            groups.append(
                {
                    "label": offer.description[:120],
                    "item_ids": [item_id],
                    "note": "Not placed in any group by the matcher; left on its own.",
                }
            )
    return groups


def _find(quotes: list[Quote], item_id: str) -> Offer | None:
    for quote in quotes:
        for offer in quote.items:
            if offer.item_id == item_id:
                return offer
    return None


def _part_numbers_settle_it(quotes: list[Quote]) -> bool:
    """Whether part numbers alone already produce a trustworthy grouping.

    Two conditions, and both are needed. Every line must carry a part number —
    one bare description and the model has real work to do. And the part numbers
    must actually overlap between suppliers, because a set that groups nothing
    is not agreement, it is three suppliers using their own internal codes, which
    is precisely the case that needs judgement.
    """
    if len(quotes) < 2:
        return False

    per_supplier: list[set[str]] = []
    for quote in quotes:
        if not quote.items:
            continue
        numbers = {
            (o.part_number or "").strip().casefold().replace(" ", "").replace("-", "")
            for o in quote.items
        }
        if "" in numbers:
            return False
        per_supplier.append(numbers)

    if len(per_supplier) < 2:
        return False
    # Every supplier must share at least one code with the first, or they are
    # not quoting the same requirement in the same language.
    return all(bool(per_supplier[0] & other) for other in per_supplier[1:])


def _fallback_groups(quotes: list[Quote]) -> list[dict[str, Any]]:
    """Group by part number, else by squashed description. No model involved."""
    buckets: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for quote in quotes:
        for offer in quote.items:
            key = (offer.part_number or "").strip().casefold().replace(" ", "").replace("-", "")
            if not key:
                key = "d:" + "".join(c for c in offer.description.casefold() if c.isalnum())
            buckets.setdefault(key, []).append(offer.item_id)
            labels.setdefault(key, offer.description[:120])
    return [
        {"label": labels[key], "item_ids": ids, "note": None} for key, ids in buckets.items()
    ]


# ── the analysis (Python's half) ───────────────────────────────────────


def analyse(
    quotes: list[Quote], groups: list[dict[str, Any]], *, currency: str = "AED"
) -> dict[str, Any]:
    """Turn matched groups into totals, rankings and findings.

    Pure and synchronous: no model, no I/O, no clock beyond the stamp. Every
    number below can be recomputed from the same inputs, which is what makes the
    saved analysis auditable.
    """
    by_item = {offer.item_id: (quote, offer) for quote in quotes for offer in quote.items}
    supplier_count = len(quotes)

    rendered_groups: list[dict[str, Any]] = []
    for group in groups:
        offers = [by_item[i] for i in group["item_ids"] if i in by_item]
        if not offers:
            continue

        # Compared per unit: suppliers routinely quote different quantities for
        # the same requirement, and line totals would then compare scope, not price.
        priced = [(q, o) for q, o in offers if o.unit_price > 0]
        cheapest = min(priced, key=lambda p: p[1].unit_price) if priced else None
        dearest = max(priced, key=lambda p: p[1].unit_price) if priced else None

        spread = None
        if cheapest and dearest and cheapest[1].unit_price > 0:
            spread = _money(
                (dearest[1].unit_price - cheapest[1].unit_price)
                / cheapest[1].unit_price
                * Decimal(100)
            )

        rendered_groups.append(
            {
                "label": group["label"],
                "note": group.get("note"),
                "quoted_by": len({q.quote_id for q, _ in offers}),
                "supplier_count": supplier_count,
                # A line only one supplier bid on is not a price comparison.
                "single_source": len({q.quote_id for q, _ in offers}) == 1,
                "offers": [
                    {
                        "quote_id": q.quote_id,
                        "supplier_name": q.supplier_name,
                        "description": o.description,
                        "part_number": o.part_number,
                        "quantity": float(o.quantity),
                        "unit_price": _out(o.unit_price),
                        "line_total": _out(o.line_total),
                        "lead_time": o.lead_time,
                        "is_best": bool(cheapest and o.item_id == cheapest[1].item_id),
                    }
                    for q, o in offers
                ],
                "best": (
                    {
                        "quote_id": cheapest[0].quote_id,
                        "supplier_name": cheapest[0].supplier_name,
                        "unit_price": _out(cheapest[1].unit_price),
                        "line_total": _out(cheapest[1].line_total),
                    }
                    if cheapest
                    else None
                ),
                "spread_pct": float(spread) if spread is not None else None,
            }
        )

    group_labels = [g["label"] for g in rendered_groups]
    suppliers = [_supplier_row(q, rendered_groups, group_labels) for q in quotes]

    complete = [s for s in suppliers if not s["missing_items"]]
    # Only suppliers who quoted everything can be ranked on total. Anyone else
    # is cheaper for a reason that has nothing to do with price.
    rankable = complete or suppliers
    cheapest_supplier = min(rankable, key=lambda s: s["total"]) if rankable else None

    split = _split_award(rendered_groups)
    saving = None
    if cheapest_supplier and split["total"] is not None:
        gap = Decimal(str(cheapest_supplier["total"])) - Decimal(str(split["total"]))
        if gap > 0:
            saving = {
                "amount": _out(gap),
                "pct": float(
                    _money(gap / Decimal(str(cheapest_supplier["total"])) * Decimal(100))
                )
                if cheapest_supplier["total"]
                else None,
                "against": cheapest_supplier["supplier_name"],
            }

    return {
        "currency": currency,
        "generated_at": datetime.now(UTC).isoformat(),
        "supplier_count": supplier_count,
        "item_count": len(rendered_groups),
        "suppliers": suppliers,
        "groups": rendered_groups,
        "cheapest_supplier": cheapest_supplier,
        "all_suppliers_complete": len(complete) == supplier_count,
        "split_award": {**split, "saving": saving},
        "insights": _insights(quotes, suppliers, rendered_groups, split, saving, currency),
    }


def _supplier_row(
    quote: Quote, groups: list[dict[str, Any]], all_labels: list[str]
) -> dict[str, Any]:
    items_total = sum((o.line_total for o in quote.items), Decimal(0))
    total = items_total - (quote.discount or Decimal(0)) + (quote.freight or Decimal(0)) + (
        quote.tax or Decimal(0)
    )

    quoted_for = {
        g["label"]
        for g in groups
        if any(o["quote_id"] == quote.quote_id for o in g["offers"])
    }

    # Their printed total against the one built from their own lines. A gap is
    # normally an unlisted discount or fee, and always worth a human look.
    mismatch = None
    if quote.quoted_total is not None and abs(quote.quoted_total - total) > Decimal("0.05"):
        mismatch = _out(quote.quoted_total - total)

    return {
        "quote_id": quote.quote_id,
        "supplier_name": quote.supplier_name,
        "currency": quote.currency,
        "fx_rate": float(quote.fx_rate),
        "converted": quote.fx_rate != Decimal(1),
        "line_count": len(quote.items),
        "items_total": _out(items_total),
        "discount": _out(quote.discount),
        "freight": _out(quote.freight),
        "tax": _out(quote.tax),
        "total": _out(total),
        "quoted_total": _out(quote.quoted_total),
        "total_mismatch": mismatch,
        "delivery_time": quote.delivery_time,
        "payment_terms": quote.payment_terms,
        "validity": quote.validity,
        "warranty": quote.warranty,
        "incoterms": quote.incoterms,
        "extraction_note": quote.extraction_note,
        "items_quoted": len(quoted_for),
        "missing_items": [label for label in all_labels if label not in quoted_for],
    }


def _split_award(groups: list[dict[str, Any]]) -> dict[str, Any]:
    """What it costs to buy each line from whoever is cheapest on it."""
    total = Decimal(0)
    by_supplier: dict[str, Decimal] = {}
    for group in groups:
        best = group.get("best")
        if not best:
            continue
        line = Decimal(str(best["line_total"] if best["line_total"] is not None else 0))
        total += line
        name = best["supplier_name"]
        by_supplier[name] = by_supplier.get(name, Decimal(0)) + line

    return {
        "total": _out(total) if groups else None,
        "by_supplier": {name: _out(value) for name, value in sorted(by_supplier.items())},
        "supplier_count": len(by_supplier),
    }


def _insights(
    quotes: list[Quote],
    suppliers: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    split: dict[str, Any],
    saving: dict[str, Any] | None,
    currency: str,
) -> list[dict[str, str]]:
    """Findings a buyer should read before deciding, incomparability first."""
    out: list[dict[str, str]] = []

    def add(kind: str, severity: str, message: str) -> None:
        out.append({"kind": kind, "severity": severity, "message": message})

    # 1. Whether these totals mean the same thing.
    for supplier in suppliers:
        missing = supplier["missing_items"]
        if missing:
            shown = ", ".join(missing[:3]) + ("..." if len(missing) > 3 else "")
            add(
                "incomplete",
                "warning",
                f"{supplier['supplier_name']} did not quote {len(missing)} of "
                f"{len(groups)} items ({shown}). Their total is not comparable "
                f"with the others as it stands.",
            )

    # 2. Whether the numbers are internally consistent.
    for supplier in suppliers:
        if supplier["total_mismatch"]:
            add(
                "total_mismatch",
                "warning",
                f"{supplier['supplier_name']}'s printed total differs from the sum of "
                f"their lines by {supplier['total_mismatch']:+,.2f} {currency}. Usually an "
                f"unlisted discount or fee — worth confirming before it is used.",
            )
        if supplier["extraction_note"]:
            add(
                "extraction",
                "warning",
                f"{supplier['supplier_name']}: {supplier['extraction_note']}",
            )
        if supplier["converted"]:
            add(
                "converted",
                "info",
                f"{supplier['supplier_name']} quoted in {supplier['currency']}, converted "
                f"at {supplier['fx_rate']} for comparison.",
            )

    # 3. Then price.
    if saving:
        add(
            "split_award",
            "info",
            f"Buying each line from whoever is cheapest on it costs "
            f"{split['total']:,.2f} {currency} across {split['supplier_count']} suppliers "
            f"— {saving['amount']:,.2f} {currency} ({saving['pct']:.1f}%) less than "
            f"{saving['against']} alone.",
        )

    for group in groups:
        if group["single_source"] and len(suppliers) > 1:
            add(
                "single_source",
                "warning",
                f"Only one supplier quoted {group['label']!r}. There is no competing "
                f"price for that line.",
            )
        elif group["spread_pct"] and Decimal(str(group["spread_pct"])) >= _NOTABLE_SPREAD_PCT:
            add(
                "spread",
                "info",
                f"{group['label']}: prices differ by {group['spread_pct']:.0f}% "
                f"— cheapest is {group['best']['supplier_name']}.",
            )

        priced = [o for o in group["offers"] if o["unit_price"]]
        if len(priced) > 1 and group["best"]:
            floor = Decimal(str(group["best"]["unit_price"] or 0))
            for offer in priced:
                if floor > 0 and Decimal(str(offer["unit_price"])) / floor >= _OUTLIER_RATIO:
                    add(
                        "outlier",
                        "warning",
                        f"{offer['supplier_name']} is "
                        f"{Decimal(str(offer['unit_price'])) / floor:.1f}x the cheapest unit "
                        f"price on {group['label']!r}. Check the pack size and currency "
                        f"before treating it as a real difference.",
                    )

    # 4. Commercial terms, which decide it when prices are close.
    deliveries = {s["supplier_name"]: s["delivery_time"] for s in suppliers if s["delivery_time"]}
    if len(set(deliveries.values())) > 1:
        add(
            "delivery",
            "info",
            "Delivery times differ: "
            + "; ".join(f"{name} {value}" for name, value in deliveries.items()),
        )
    if missing_validity := [s["supplier_name"] for s in suppliers if not s["validity"]]:
        add(
            "validity",
            "info",
            f"No validity period stated by {', '.join(missing_validity)}. Worth asking "
            f"how long the price holds.",
        )

    return out


__all__ = ["ExtractionError", "Offer", "Quote", "analyse", "match_items"]
