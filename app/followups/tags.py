"""What kind of reason a late task was given, by rules.

The user's shortlist (2026-11-18, #16): each reason is tagged so the daily
report can say "waiting on suppliers 3, customer clarification 1" rather than
only list them. Keywords, first match wins, in the order below; anything else
is "other". Set when the reason is filed (``service.answer``).
"""

from __future__ import annotations

import re
from typing import Final

#: tag → (what the report calls it, the words that mean it).
TAGS: Final[tuple[tuple[str, str, re.Pattern[str]], ...]] = (
    ("declined", "declined / no bid", re.compile(
        r"\b(declin\w*|regret\w*|no[\s-]?bid|not\s+(?:participat\w*|interested|bidding|"
        r"submitting)|drop(?:ped)?)\b", re.I)),
    ("portal", "portal or system", re.compile(
        r"\b(portal|ariba|login|log\s?in|upload\w*|site\s+(?:down|issue)|technical|"
        r"system\s+(?:issue|error|down)|error|access)\b", re.I)),
    ("approval", "waiting for approval", re.compile(
        r"\b(approv\w*|sign[\s-]?off|management|decision|confirm\w*\s+from|go[\s-]?ahead)\b",
        re.I)),
    ("customer", "customer / clarification", re.compile(
        r"\b(customer|client|end[\s-]?user|buyer|clarif\w*|query|queries|extension|"
        r"extended|postpon\w*|rfq\s+(?:changed|revised)|addendum)\b", re.I)),
    ("supplier", "waiting on supplier", re.compile(
        r"\b(supplier|vendor|distributor|principal|oem|price|pricing|quot\w*|"
        r"no\s+(?:response|reply)|partner|manufacturer|stock)\b", re.I)),
    ("workload", "workload / time", re.compile(
        r"\b(busy|workload|other\s+tasks?|priorit\w*|leave|sick|time|too\s+many|"
        r"handover|overload\w*)\b", re.I)),
)
LABELS: Final = {key: label for key, label, _ in TAGS} | {"other": "other"}


def tag(reason: str | None) -> str | None:
    """The reason's kind, or None for no reason."""
    text = (reason or "").strip()
    if not text:
        return None
    for key, _, pattern in TAGS:
        if pattern.search(text):
            return key
    return "other"


def counted(tags: list[str | None]) -> str:
    """ "waiting on supplier 2 · customer / clarification 1", most first."""
    counts: dict[str, int] = {}
    for t in tags:
        if t:
            counts[t] = counts.get(t, 0) + 1
    return " · ".join(f"{LABELS.get(k, k)} {n}"
                      for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
