"""Comparing the quotes once they have been read.

The work splits in two, and the split is the whole design:

**Matching is done by the identifiers and the words.** "Filter element FX-200",
"FX200 Filter Elm." and "Element, filter, FX 200" are one item, and the thing
that says so is the part number — normalised so that FX-200, FX 200 and FX200
are one code, while FX-200H stays apart. Where there is no part number the
descriptions are compared word for word, conservatively: two groups that should
have been one is a visible, harmless result, and one group that should have
been two is a wrong price comparison. A model used to make this call. It is
gone, and the rules below are written to be wrong in the harmless direction.

**Everything numeric is a Python problem**, so Python does it. Totals, spreads,
the split-award figure and every saving in here are computed from the matched
groups in ``Decimal``.

The insights are deliberately blunt about *incomparability*. The most expensive
mistake in a quote comparison is not picking the wrong supplier, it is comparing
two totals that were never for the same scope: supplier A looks 12% cheaper
because they quietly left two lines out. Coverage is checked before price, and a
supplier who did not quote everything is flagged rather than ranked.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from app.comparison.extraction import ExtractionError, QuoteExtractor

logger = logging.getLogger("hamdaz.comparison")

_CENTS: Final = Decimal("0.01")

#: A unit price this far above the cheapest offer for the same item is called
#: out. Usually a genuine premium; sometimes a pack-size or currency misreading,
#: which is exactly why it is worth a look.
_OUTLIER_RATIO: Final = Decimal("2.0")

#: Below this the spread on a line is noise rather than a finding.
_NOTABLE_SPREAD_PCT: Final = Decimal("15")

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


# ── matching ───────────────────────────────────────────────────────────

#: Descriptions are compared on their words. Below this share of words in
#: common two descriptions are different things, however alike they look.
_SAME_WORDS: Final = 0.6

#: Words that say nothing about what an item is. Left out of the comparison so
#: "Supply of filter" and "Filter, supply and delivery" compare on "filter".
_NOISE: Final = frozenset(
    {
        "a", "an", "and", "the", "of", "for", "with", "to", "in", "or", "per",
        "supply", "supplying", "delivery", "installation", "each", "pcs", "pc",
        "nos", "no", "set", "sets", "unit", "units", "item", "items", "type",
    }
)


def normalise_code(code: str | None) -> str:
    """A part number as an identifier: case, spaces and punctuation gone.

    FX-200, "FX 200" and fx200 are one code. FX-200H is another — the suffix
    survives, which is the whole point of comparing codes rather than words.
    """
    return re.sub(r"[^a-z0-9]", "", (code or "").casefold())


def _words(description: str) -> tuple[set[str], set[str]]:
    """The words of a description, and the ones among them that carry a model
    number. ``fx-200`` and ``fx 200`` both become ``fx200`` first, so a code
    written two ways is one word."""
    text = description.casefold()
    text = re.sub(r"(?<=[a-z])[\s\-/](?=\d)", "", text)
    text = re.sub(r"(?<=\d)[\s\-/](?=[a-z])", "", text)
    tokens = {t for t in re.split(r"[^a-z0-9.]+", text) if t and t not in _NOISE}
    tokens = {t.strip(".") for t in tokens if t.strip(".")}
    coded = {t for t in tokens if any(c.isdigit() for c in t)}
    return tokens, coded


def _same_item(a: Offer, b: Offer) -> bool:
    """Whether two lines from different suppliers are the same requirement.

    Part numbers decide when both have one. Otherwise the descriptions must
    agree on every model number they mention and share most of their words.
    Conservative on purpose: a miss shows as two rows, a false match as a wrong
    price comparison, and only one of those loses money.
    """
    code_a, code_b = normalise_code(a.part_number), normalise_code(b.part_number)
    if code_a and code_b:
        return code_a == code_b
    words_a, coded_a = _words(a.description)
    words_b, coded_b = _words(b.description)
    if not words_a or not words_b:
        return False
    # One side's part number may be in the other's description — "FX-200" in
    # "Filter element FX-200" — which is as good as a code on both.
    if code_a and code_a in {normalise_code(w) for w in words_b}:
        return True
    if code_b and code_b in {normalise_code(w) for w in words_a}:
        return True
    if coded_a != coded_b:
        return False
    shared = len(words_a & words_b)
    return shared / len(words_a | words_b) >= _SAME_WORDS


def match_locally(quotes: list[Quote]) -> list[dict[str, Any]]:
    """Group equivalent line items across suppliers, by the rules above.

    A line is only ever grouped with lines from *other* suppliers — two lines on
    one quote are two things the supplier is selling, whatever they are called.
    Groups are built greedily in document order, and a line that matches
    nothing stays on its own, which is the honest result for a line only one
    supplier bid on.
    """
    groups: list[dict[str, Any]] = []
    members: list[list[Offer]] = []
    for quote in quotes:
        for offer in quote.items:
            placed = False
            for index, group in enumerate(members):
                if any(m.quote_id == offer.quote_id for m in group):
                    continue
                if all(_same_item(offer, m) for m in group):
                    group.append(offer)
                    groups[index]["item_ids"].append(offer.item_id)
                    placed = True
                    break
            if not placed:
                members.append([offer])
                groups.append(
                    {"label": offer.description[:120], "item_ids": [offer.item_id], "note": None}
                )
    for group, offers in zip(groups, members, strict=True):
        if len(offers) > 1 and not all(normalise_code(o.part_number) for o in offers):
            group["note"] = (
                "Matched on the description rather than a part number. "
                "Check the lines are for the same item."
            )
    return groups


async def match_items(extractor: QuoteExtractor, quotes: list[Quote]) -> list[dict[str, Any]]:
    """Group equivalent line items across suppliers.

    ``extractor`` is accepted for the callers written when a model did this,
    and ignored. Async for the same reason — the shape of the call did not
    change when what it does did.
    """
    del extractor
    if not any(quote.items for quote in quotes):
        return []
    return match_locally(quotes)


def _find(quotes: list[Quote], item_id: str) -> Offer | None:
    for quote in quotes:
        for offer in quote.items:
            if offer.item_id == item_id:
                return offer
    return None


def _part_numbers_settle_it(quotes: list[Quote]) -> bool:
    """Whether part numbers alone already produce a trustworthy grouping.

    Two conditions, and both are needed. Every line must carry a part number —
    one bare description and the words have real work to do. And the part
    numbers must actually overlap between suppliers, because a set that groups
    nothing is not agreement, it is three suppliers using their own internal
    codes, which is precisely the case that needs the descriptions.
    """
    if len(quotes) < 2:
        return False

    per_supplier: list[set[str]] = []
    for quote in quotes:
        if not quote.items:
            continue
        numbers = {normalise_code(o.part_number) for o in quote.items}
        if "" in numbers:
            return False
        per_supplier.append(numbers)

    if len(per_supplier) < 2:
        return False
    # Every supplier must share at least one code with the first, or they are
    # not quoting the same requirement in the same language.
    return all(bool(per_supplier[0] & other) for other in per_supplier[1:])


def _fallback_groups(quotes: list[Quote]) -> list[dict[str, Any]]:
    """Group by part number, else by squashed description. The bluntest rule,
    kept for the tests that pin the arithmetic to a known grouping."""
    buckets: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for quote in quotes:
        for offer in quote.items:
            key = normalise_code(offer.part_number)
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
