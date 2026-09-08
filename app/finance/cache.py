"""A short shared cache over the ledger sweep.

The sweep is by far the most expensive thing this app does to Zoho: a year's
statement reads invoices, credit notes, bills, vendor credits, expenses and
journals, each of them paged, against an organisation-wide budget of 100 requests
a minute that the quotes module is also spending. Two managers opening the same
month must not cost twice.

Cached at the *sweep* rather than at the statement, which is the whole point:
the statement, the monthly trend and every account drill-down are all built from
one set of postings, so caching the raw rows serves all three from one read.
Rebuilding a statement from cached rows is arithmetic on data already in memory.

The figures are also identical for every viewer — a P&L is not scoped per person
— so one entry serves everyone who may see it at all. Who *may* is decided in the
router, before this is reached.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final

from app.finance.service import Period, Sweep, sweep

#: Longer than the quotes cache. Yesterday's ledger does not change, a closed
#: month changes not at all, and a P&L is read as a considered figure rather
#: than as a live feed — nobody raises an invoice and refreshes to watch profit
#: move. Five minutes keeps a working session off Zoho almost entirely.
CACHE_TTL_SECONDS: Final = 300

#: Distinct periods worth remembering. A dashboard showing this month, last
#: month and the year, each against its comparison, is six; the rest is room for
#: people looking at custom ranges.
_MAX_ENTRIES: Final = 24


class PnlCache:
    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple[str, str], tuple[float, Sweep]] = {}
        # One sweep per period at a time. Without this, a dashboard that opens
        # four panels at once starts four identical sweeps and spends the
        # minute's entire budget rendering one screen.
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _fresh(self, key: tuple[str, str]) -> Sweep | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at >= self._ttl:
            return None
        return value

    async def sweep(self, zoho, period: Period, *, refresh: bool = False) -> Sweep:
        """The rows for ``period``, read once however many callers want them."""
        key = (period.start.isoformat(), period.end.isoformat())

        if not refresh and (hit := self._fresh(key)) is not None:
            return hit

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not refresh and (hit := self._fresh(key)) is not None:
                return hit

            result = await sweep(zoho, period)
            self._entries[key] = (time.monotonic(), result)
            self._evict()
            return result

    def _evict(self) -> None:
        while len(self._entries) > _MAX_ENTRIES:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]
            self._locks.pop(oldest, None)
