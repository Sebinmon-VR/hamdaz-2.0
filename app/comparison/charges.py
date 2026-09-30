"""The charges a supplier puts on top of the lines, read from anywhere in the text.

A quotation prices its lines in a table and then, as often as not, says the
rest in its notes: "Customs duty 5% extra", "Handling charges AED 150",
"Prices exclusive of installation", "Delivered duty paid". The table reader
never saw those sentences, so the duty a supplier had plainly said was extra
was costed at the house rate, or not at all.

This reads every line of the document's text, table rows and notes alike, for
the charges a landed cost is built from: duty, customs clearance, handling,
packing, insurance, documentation, installation, bank charges. Freight is not
here. It has its own figure on the quote (see ``parsing._TOTALS``).

Each charge says what the supplier said about it:

* an **amount**: "Handling charges: USD 150"
* a **percent**: "Customs duty @ 5% extra"
* **included**: "Prices inclusive of customs duty", "DDP", "Not applicable"
* **extra, amount not stated**: "Duty and clearance at actuals", "excluded"

A mention with none of these ("Duty as per HS code") is not a charge, and is
left alone. Nothing here calculates. What the costing does with a charge is
``app.quoting.costing.seed_costing``'s business.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, Field

#: What a charge is, and the words a supplier uses for it. Order matters only
#: for reading: the longer phrase is tried first, so "customs clearance" is
#: clearance rather than a duty.
KINDS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "clearance",
        re.compile(
            r"\b(?:customs\s+clearance|clearance\s+(?:charges?|fees?|cost)|clearing\s+charges?"
            r"|clearing\s+agent)\b",
            re.I,
        ),
    ),
    (
        "duty",
        re.compile(
            r"\b(?:customs?\s+dut(?:y|ies)|import\s+dut(?:y|ies)|dut(?:y|ies)(?:\s+and\s+taxes)?)\b",
            re.I,
        ),
    ),
    ("handling", re.compile(r"\bhandling\b", re.I)),
    ("packing", re.compile(r"\b(?:packing|packaging|crating)\b", re.I)),
    ("insurance", re.compile(r"\binsurance\b", re.I)),
    (
        "documentation",
        re.compile(
            r"\b(?:documentation|document\s+charges?|legali[sz]ation|attestation"
            r"|certificate\s+of\s+origin|coo\s+charges?)\b",
            re.I,
        ),
    ),
    (
        "installation",
        re.compile(r"\b(?:installation|commissioning|on-?site\s+support)\b", re.I),
    ),
    ("bank", re.compile(r"\bbank\s+(?:charges?|fees?)\b", re.I)),
)

LABELS: Final[dict[str, str]] = {
    "clearance": "Customs clearance",
    "duty": "Customs duty",
    "handling": "Handling",
    "packing": "Packing",
    "insurance": "Insurance",
    "documentation": "Documentation",
    "installation": "Installation",
    "bank": "Bank charges",
}

#: Freight is read elsewhere, but it still bounds a charge's words: in
#: "Freight & handling USD 300" the 300 is not the handling on its own.
_FREIGHT: Final = re.compile(r"\b(?:freight|shipping|courier|delivery\s+charges?)\b", re.I)

#: "Not ours to pay on top": the supplier's price already carries it.
_INCLUDED: Final = re.compile(
    r"\b(?:inclusive|incl\b\.?|included|including|duty\s+paid|ddp|not\s+applicable|n/a|nil"
    r"|free\s+of\s+charge|foc|waived)",
    re.I,
)
#: "On top, and yours": said without a figure, it is still a cost to allow for.
_EXTRA: Final = re.compile(
    r"\b(?:extra|excl(?:uding|uded|usive|\.)?|exclusive\s+of|not\s+included|not\s+inclusive"
    r"|additional(?:ly)?|at\s+actuals?|as\s+per\s+actuals?|chargeable|to\s+be\s+borne"
    r"|borne\s+by|on\s+(?:the\s+)?(?:buyer|customer|client)|payable\s+by|to\s+your\s+account"
    r"|by\s+(?:the\s+)?(?:buyer|customer|client|consignee))\b",
    re.I,
)

#: A number, not a percentage and not a duration. "10%" must not read as a
#: 1 once the regex backtracks, so the check is made on what follows the match.
_NUMBER: Final = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\d.,])\d+(?:\.\d+)?")
_PERCENT_AFTER: Final = re.compile(r"\s*%")
_NOT_MONEY_AFTER: Final = re.compile(
    r"\s*(?:(?:-|–|to)\s*\d+\s*)?(?:days?|weeks?|wks?|months?|hours?|hrs?|years?|yrs?"
    r"|working|business|kgs?|pcs|nos|units?|x\b)",
    re.I,
)
#: A code or a reference, not money: "HS code 8471.30", "clause 12".
_NOT_MONEY_BEFORE: Final = re.compile(
    r"(?:\bhs|\bcode|\bclause|\barticle|\bsection|\bref|\bno\.?|#|\bitem|\bline)\s*[:.\-]?\s*$",
    re.I,
)

#: Sentences, so that "Duty included. Handling AED 200 extra." is two answers.
_SENTENCE: Final = re.compile(r"(?<=[.;])\s+(?=[A-Z(*\-•])|\s*;\s*|\s+\|\s+(?=[A-Za-z])")


class ExtractedCharge(BaseModel):
    """One charge the supplier puts on top of their lines."""

    kind: str = Field(description="duty, clearance, handling, packing, insurance, …")
    label: str = Field(description="The words on the document, trimmed")
    amount: float | None = Field(default=None, description="As printed, in the quote's currency")
    percent: float | None = Field(default=None, description="As printed, e.g. 5 for 5%")
    #: ``cif`` when the supplier says so ("1% of CIF value"); otherwise goods.
    percent_of: str = "goods"
    #: The supplier's price already carries it: nothing to add.
    included: bool = False


def _clauses(line: str) -> list[str]:
    return [c.strip(" -*•|\t") for c in _SENTENCE.split(line) if c and c.strip(" -*•|\t")]


def money_in(text: str) -> list[Decimal]:
    """The amounts in ``text``, skipping percentages, durations and codes."""
    from app.comparison.parsing import to_number

    found: list[Decimal] = []
    for match in _NUMBER.finditer(text):
        after = text[match.end() :]
        if _PERCENT_AFTER.match(after) or _NOT_MONEY_AFTER.match(after):
            continue
        if _NOT_MONEY_BEFORE.search(text[: match.start()]):
            continue
        if (value := to_number(match.group(0))) is not None:
            found.append(value)
    return found


def _percent_in(text: str) -> Decimal | None:
    match = re.search(r"(?<![\d.])(\d{1,2}(?:\.\d{1,3})?)\s*%", text)
    return Decimal(match.group(1)) if match else None


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def find_charges(text: str, item_descriptions: list[str] | None = None) -> list[ExtractedCharge]:
    """Every charge the document states, one per kind, first statement wins.

    ``item_descriptions`` are the priced lines already read: a row that *is* a
    line ("Installation and commissioning, 1 lot, 500") is the supplier
    selling a service, not a charge on top, and counting it twice would price
    it twice.
    """
    if not text:
        return []
    items = [_squash(d) for d in (item_descriptions or []) if len(_squash(d)) >= 6]
    found: dict[str, ExtractedCharge] = {}

    for line in text.splitlines():
        squashed = _squash(line)
        if not squashed or any(item in squashed for item in items):
            continue
        for clause in _clauses(line):
            labels = sorted(
                [(m.start(), m.end(), kind) for kind, rx in KINDS for m in rx.finditer(clause)]
                + [(m.start(), m.end(), "freight") for m in _FREIGHT.finditer(clause)]
            )
            # A phrase matched by two kinds ("customs clearance" is also near
            # "customs duty") keeps the first, longer reading.
            kept: list[tuple[int, int, str]] = []
            for start, end, kind in labels:
                if kept and start < kept[-1][1]:
                    continue
                kept.append((start, end, kind))
            for i, (_start, end, kind) in enumerate(kept):
                if kind == "freight" or kind in found:
                    continue
                if i > 0 and kept[i - 1][2] == "freight":
                    between = clause[kept[i - 1][1] : _start]
                    if not money_in(between) and not _percent_in(between):
                        # "Freight & handling USD 300": one figure for both, and
                        # it is the freight's (``parsing._TOTALS``).
                        continue
                stop = kept[i + 1][0] if i + 1 < len(kept) else len(clause)
                segment = clause[end:stop]
                # "Freight & handling USD 300": the figure is after both names.
                # A shared figure is not this charge's own, so only the words
                # after this label and before the next one are asked.
                if i + 1 < len(kept) and not money_in(segment) and not _percent_in(segment):
                    tail = clause[kept[-1][1] :]
                    shared = bool(money_in(tail) or _percent_in(tail))
                else:
                    shared = False
                percent = _percent_in(segment)
                amounts = money_in(segment)
                amount = amounts[-1] if amounts and percent is None else None
                said = segment if (_INCLUDED.search(segment) or _EXTRA.search(segment)) else clause
                extra = bool(_EXTRA.search(said))
                included = not extra and bool(_INCLUDED.search(said))
                if amount is None and percent is None and not extra and not included:
                    continue
                if shared and not extra and not included:
                    continue
                found[kind] = ExtractedCharge(
                    kind=kind,
                    label=clause.strip()[:300],
                    amount=float(amount) if amount is not None and amount > 0 else None,
                    percent=float(percent) if percent is not None and percent > 0 else None,
                    percent_of="cif" if re.search(r"\bcif\b", segment, re.I) else "goods",
                    included=included and amount is None and percent is None,
                )
    return list(found.values())
