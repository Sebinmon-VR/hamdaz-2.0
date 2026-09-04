"""A short shared cache over the quotes list.

Sweeping every quote is the expensive call in this module — 400-odd rows across
several pages, and Zoho allows the whole organisation only 100 requests a minute.
The list is also identical for every caller, since quotes are not scoped per
person, so one cache serves everyone.

Keyed by the filters, because ``?status=draft`` and an unfiltered sweep are
genuinely different upstream queries and must not shadow each other. Detail
lookups are deliberately *not* cached: they are one call each, they are what a
person looks at when they want the current state, and stale line items would be
worse than a second's wait.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Final

from app.zoho.client import MAX_ROWS as _CEILING
from app.zoho.client import ZohoBooks

#: Short enough that a quote raised in Zoho shows up while somebody is still
#: looking for it.
CACHE_TTL_SECONDS: Final = 60

#: Distinct filter combinations worth remembering. Beyond this the oldest go;
#: without a bound, a client varying ``search`` would grow this without limit.
_MAX_ENTRIES: Final = 32


class QuoteCache:
    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple, tuple[float, list[dict], bool]] = {}
        # One refresh per key at a time: without this, ten simultaneous cold
        # requests become ten full sweeps and exhaust the minute's budget.
        self._locks: dict[tuple, asyncio.Lock] = {}

    def _fresh(self, key: tuple) -> tuple[list[dict], bool] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, rows, truncated = entry
        if time.monotonic() - stored_at >= self._ttl:
            return None
        return rows, truncated

    async def quotes(
        self, zoho: ZohoBooks, *, refresh: bool = False, limit: int | None = None, **filters: Any
    ) -> tuple[list[dict], bool]:
        """The quotes matching ``filters``, and whether the sweep was cut short.

        ``limit=None`` means every quote there is — the ordinary case, since the
        point of the list is to be the list.
        """
        key = (limit, *sorted((k, v) for k, v in filters.items() if v is not None))

        if not refresh and (hit := self._fresh(key)) is not None:
            return hit

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Somebody else may have filled it while we queued.
            if not refresh and (hit := self._fresh(key)) is not None:
                return hit

            rows = await zoho.estimates(limit=limit, **filters)
            # True only when something actually cut the sweep short: the caller's
            # own limit, or the client's page ceiling. An unlimited sweep that
            # ran to the end is complete, and must not claim otherwise.
            truncated = (limit is not None and len(rows) >= limit) or len(rows) >= _CEILING
            self._entries[key] = (time.monotonic(), rows, truncated)
            self._evict()
            return rows, truncated

    def _evict(self) -> None:
        while len(self._entries) > _MAX_ENTRIES:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]
            self._locks.pop(oldest, None)
