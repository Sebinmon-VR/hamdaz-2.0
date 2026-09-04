"""How likely this quote is to be won, from what actually happened before.

Read out of the Zoho Books estimate history, which is the only record of
outcomes there is. Read-only — nothing is written to Zoho.

**What counts as decided.** An estimate that was accepted or invoiced was won;
one that was declined or expired was lost. Draft and sent are undecided and are
excluded entirely, which matters here: 65% of the history is draft, and counting
those as losses would put every probability near zero for no reason other than
that nobody tidies up.

**Why the rate is smoothed.** A customer with two decided quotes, both won, is
not a 100% customer — it is a customer we know almost nothing about. So the
customer's own rate is pulled toward the organisation's overall rate in
proportion to how little evidence there is:

    p = (won + k · baseline) / (decided + k)

With ``k`` at 10, a customer with 60 decided quotes is judged almost entirely on
their own record, and one with 2 is judged almost entirely on the baseline. This
is the standard fix for small samples, and without it the number would be most
confident exactly where it is least justified.

Every result carries the counts it came from, because a probability without its
sample size invites more trust than it has earned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from app.zoho.client import ZohoBooks, ZohoError

logger = logging.getLogger("hamdaz.quoting")

#: Outcomes that settle a quote either way. Anything else is still open.
WON: Final = frozenset({"accepted", "invoiced"})
LOST: Final = frozenset({"declined", "expired"})

#: Strength of the pull toward the organisation baseline, in "quotes worth" of
#: evidence. 10 means a customer needs roughly ten decided quotes before their
#: own record outweighs the baseline.
SMOOTHING: Final = 10

#: Used when the history has nothing to say at all.
FALLBACK_BASELINE: Final = 0.5


@dataclass(frozen=True, slots=True)
class WinEstimate:
    probability: Decimal
    basis: dict[str, Any]


class WinRates:
    """Win rates over the estimate history, cached for a short while.

    The sweep is ~1,700 rows and identical for every caller, so it is shared
    rather than repeated per quote being drafted.
    """

    def __init__(self, ttl_seconds: int = 900) -> None:
        self._ttl = ttl_seconds
        self._at = 0.0
        self._baseline = FALLBACK_BASELINE
        self._by_customer: dict[str, tuple[int, int]] = {}
        self._decided = 0

    async def _load(self, zoho: ZohoBooks, *, refresh: bool = False) -> None:
        import time

        if not refresh and self._by_customer and time.monotonic() - self._at < self._ttl:
            return

        rows = await zoho.estimates()
        by_customer: dict[str, tuple[int, int]] = {}
        won = decided = 0

        for row in rows:
            status = (row.get("status") or "").strip().casefold()
            if status in WON:
                outcome = 1
            elif status in LOST:
                outcome = 0
            else:
                continue  # undecided: says nothing either way

            decided += 1
            won += outcome
            name = (row.get("customer_name") or "").strip().casefold()
            if name:
                w, d = by_customer.get(name, (0, 0))
                by_customer[name] = (w + outcome, d + 1)

        self._baseline = won / decided if decided else FALLBACK_BASELINE
        self._by_customer = by_customer
        self._decided = decided
        self._at = time.monotonic()
        logger.info(
            "win rates: %d decided quotes, baseline %.0f%%, %d customers",
            decided,
            self._baseline * 100,
            len(by_customer),
        )

    async def estimate(
        self, zoho: ZohoBooks, *, customer_name: str | None, refresh: bool = False
    ) -> WinEstimate:
        """This customer's chance, smoothed toward the organisation's."""
        try:
            await self._load(zoho, refresh=refresh)
        except ZohoError as exc:
            # A quote must still be saveable when the history is unreachable.
            logger.warning("win rates unavailable: %s", exc)
            return WinEstimate(
                Decimal(str(round(FALLBACK_BASELINE, 4))),
                {
                    "source": "unavailable",
                    "reason": f"Could not read the estimate history: {exc}",
                    "baseline": FALLBACK_BASELINE,
                },
            )

        name = (customer_name or "").strip().casefold()
        won, decided = self._by_customer.get(name, (0, 0))

        smoothed = (won + SMOOTHING * self._baseline) / (decided + SMOOTHING)

        return WinEstimate(
            Decimal(str(round(smoothed, 4))),
            {
                "source": "zoho estimate history",
                "customer": customer_name,
                "customer_decided": decided,
                "customer_won": won,
                # The customer's raw rate, shown next to the smoothed one so the
                # adjustment is visible rather than mysterious.
                "customer_raw_rate": round(won / decided, 4) if decided else None,
                "organisation_baseline": round(self._baseline, 4),
                "organisation_decided": self._decided,
                "smoothing": SMOOTHING,
                "note": (
                    f"{decided} decided quote(s) for this customer. "
                    + (
                        "Too few to judge on their own, so this is mostly the "
                        "organisation's overall rate."
                        if decided < SMOOTHING
                        else "Enough history to lean on the customer's own record."
                    )
                    + " Draft and sent quotes are excluded — they decided nothing."
                ),
            },
        )
