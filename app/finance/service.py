"""Reading Zoho and handing the result to the P&L builder.

Two rules, both inherited from ``app.zoho.service`` because they were right
there and are right here:

**A source fails alone.** If the token cannot read vendor credits, the statement
should still show revenue and costs, say that vendor credits are missing, and
mark itself incomplete. The alternative — one 403 blanking the whole report — is
how a permissions problem in a corner of Zoho becomes "the finance module is
down".

**Nothing is fetched twice.** A statement and its comparison period share a
chart of accounts, and the sweeps behind them are the expensive part. Zoho allows
the organisation 100 requests a minute in total, shared with the quotes module,
so a careless refresh here degrades an unrelated screen.

The period is always resolved to explicit dates before anything is read, so a
statement records the dates it actually covers rather than the phrase somebody
typed. "This quarter" means something different on the last day of March.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from app.finance.accounts import Chart
from app.finance.ledger import WALKERS, Walk
from app.finance.pnl import Statement, build
from app.zoho.catalogue import BY_KEY, LEDGER, SWEEPABLE, Endpoint
from app.zoho.client import ZohoBooks, ZohoError, ZohoScopeError

logger = logging.getLogger("hamdaz.finance")

#: Named periods a caller can ask for instead of two dates.
PRESETS: Final[tuple[str, ...]] = (
    "this_month",
    "last_month",
    "this_quarter",
    "last_quarter",
    "this_year",
    "last_year",
    "last_12_months",
)


@dataclass(frozen=True, slots=True)
class Period:
    start: date
    end: date
    label: str

    def previous(self) -> Period:
        """The comparison period: the same length, immediately before.

        Length-based rather than calendar-based on purpose. A calendar rule
        ("the previous month") is ambiguous for a period that is not a whole
        month, and every attempt to be clever about 28th–31st ends up comparing
        a 31-day month against a 28-day one and calling the difference growth.
        """
        span = (self.end - self.start).days
        end = self.start - _ONE_DAY
        return Period(
            end - timedelta(days=span), end, f"{span + 1} days to {end.isoformat()}"
        )


_ONE_DAY: Final = timedelta(days=1)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def resolve(
    preset: str | None, start: date | None, end: date | None, *, today: date | None = None
) -> Period:
    """Turn a preset or a pair of dates into one explicit period.

    Explicit dates win when both are given. ``today`` is injectable so the
    presets can be tested without waiting for a month to end.
    """
    now = today or datetime.now(UTC).date()

    if start and end:
        if end < start:
            raise ValueError("The end of the period cannot precede its start")
        return Period(start, end, f"{start.isoformat()} to {end.isoformat()}")
    if start or end:
        raise ValueError("Give both start and end, or neither")

    key = (preset or "this_month").strip().casefold()

    if key == "this_month":
        return Period(now.replace(day=1), _month_end(now.year, now.month), "This month")
    if key == "last_month":
        first = now.replace(day=1) - _ONE_DAY
        return Period(first.replace(day=1), first, "Last month")
    if key in ("this_quarter", "last_quarter"):
        quarter = (now.month - 1) // 3
        if key == "last_quarter":
            quarter -= 1
        year = now.year + (quarter // 4)
        quarter %= 4
        first_month = quarter * 3 + 1
        return Period(
            date(year, first_month, 1),
            _month_end(year, first_month + 2),
            "This quarter" if key == "this_quarter" else "Last quarter",
        )
    if key == "this_year":
        return Period(date(now.year, 1, 1), date(now.year, 12, 31), "This year")
    if key == "last_year":
        return Period(date(now.year - 1, 1, 1), date(now.year - 1, 12, 31), "Last year")
    if key == "last_12_months":
        start_month = now.replace(day=1)
        for _ in range(11):
            start_month = (start_month - _ONE_DAY).replace(day=1)
        return Period(start_month, _month_end(now.year, now.month), "Last 12 months")

    raise ValueError(f"Unknown period {preset!r}. Use one of: {', '.join(PRESETS)}")


#: The organisation's base currency, looked up once per process.
#:
#: Every figure in a statement is already converted into it by the exchange rate
#: on each document, so this is a label rather than a calculation — but a P&L
#: headed with the wrong currency symbol is worse than one headed with none.
#: Cached in a module global because it is a property of the organisation and
#: changes about never; the alternative is an extra upstream call on every
#: statement, for a three-letter string.
_BASE_CURRENCY: str | None = None


async def base_currency(zoho: ZohoBooks) -> str:
    """The organisation's base currency code, or an empty string.

    Failure is deliberately not an error. Not being able to read
    ``/organizations`` is a reason to omit a currency label, never a reason to
    withhold a profit and loss that was computed successfully.
    """
    global _BASE_CURRENCY
    if _BASE_CURRENCY is not None:
        return _BASE_CURRENCY

    try:
        rows = await zoho.list_rows(BY_KEY["organizations"], limit=5)
    except ZohoError as exc:
        logger.info("finance could not read the base currency: %s", exc)
        return ""

    # /organizations lists every organisation the token can see, which for a
    # multi-entity Zoho account is more than one. Matching on the configured id
    # rather than taking the first is the difference between labelling the
    # statement AED and labelling it with whichever entity Zoho happened to
    # return first.
    match = next(
        (r for r in rows if str(r.get("organization_id")) == zoho.organization_id),
        rows[0] if rows else None,
    )
    _BASE_CURRENCY = (match or {}).get("currency_code") or ""
    return _BASE_CURRENCY


@dataclass(slots=True)
class Sweep:
    """Raw rows for one period, and what could not be read."""

    chart: Chart
    rows: dict[str, list[dict]]
    missing: dict[str, str]

    @property
    def counts(self) -> dict[str, int]:
        return {key: len(value) for key, value in self.rows.items()}


async def _read(zoho: ZohoBooks, endpoint: Endpoint, params: dict) -> list[dict]:
    return await zoho.list_rows(endpoint, params=params)


async def sweep(zoho: ZohoBooks, period: Period) -> Sweep:
    """Read the chart of accounts and every ledger source for one period.

    The chart comes first and alone, because without it no posting can be
    classified and the rest of the work would be wasted. Everything after it goes
    at once — they are independent reads, and doing them in sequence would take
    six round trips of latency for no benefit.
    """
    accounts_endpoint = BY_KEY["chartofaccounts"]
    try:
        chart_rows = await zoho.list_rows(accounts_endpoint)
    except ZohoError as exc:
        # Fatal, unlike a missing source: with no chart there is no way to tell
        # an income account from a bank account, and every figure would be zero.
        raise ZohoError(
            f"The chart of accounts could not be read, so no profit and loss can "
            f"be built: {exc}"
        ) from exc

    chart = Chart.from_zoho(chart_rows)

    # Zoho honours these on some endpoints and ignores them on others. They are
    # here to keep the sweep small; app.finance.ledger filters by date again
    # locally, so correctness does not depend on which behaviour we got.
    window = {
        "date_start": period.start.isoformat(),
        "date_end": period.end.isoformat(),
    }

    results = await asyncio.gather(
        *(_read(zoho, endpoint, window) for endpoint in LEDGER),
        return_exceptions=True,
    )

    rows: dict[str, list[dict]] = {}
    missing: dict[str, str] = {}
    for endpoint, result in zip(LEDGER, results, strict=True):
        if isinstance(result, ZohoScopeError):
            # The exception already phrases this correctly — a refusal is either
            # the scope or the authorising user's Books role, and Zoho does not
            # say which. Restating it here as "add the scope" is how the wrong
            # advice got into the response the first time.
            missing[endpoint.key] = f"{endpoint.name} could not be read. {result}"
            logger.info("finance sweep %s refused: %s", endpoint.key, result)
        elif isinstance(result, BaseException):
            missing[endpoint.key] = f"{endpoint.name} could not be read: {result}"
            logger.warning("finance sweep %s failed: %s", endpoint.key, result)
        else:
            rows[endpoint.key] = result

    return Sweep(chart=chart, rows=rows, missing=missing)


def statement_from(sweep_result: Sweep, period: Period, currency: str) -> tuple[Statement, Walk]:
    """Walk the swept rows into a statement.

    Returns the ``Walk`` alongside it because the drill-down and the monthly
    trend are built from the same postings. Recomputing them would mean a second
    sweep of Zoho for data already in memory.
    """
    walk = Walk()
    for key, rows in sweep_result.rows.items():
        walker = WALKERS.get(key)
        if walker is None:
            # A ledger endpoint was added to the catalogue without a walker.
            # Silently ignoring it would under-report profit, so it is reported
            # as missing exactly like an unreadable source.
            sweep_result.missing[key] = f"No ledger rule is defined for {key}"
            continue
        walk.extend(walker(rows, sweep_result.chart, (period.start, period.end)))

    statement = build(
        sweep_result.chart,
        walk,
        start=period.start,
        end=period.end,
        currency=currency,
        counts=sweep_result.counts,
        missing=sweep_result.missing,
    )
    return statement, walk


async def diagnostics(zoho: ZohoBooks) -> list[dict]:
    """Probe every sweepable endpoint and report what this token can actually read.

    Worth its own endpoint because the two things most likely to be wrong here
    are invisible from the outside: an OAuth scope nobody granted, and a response
    key in the catalogue that was inferred rather than verified. This answers
    both at once — one row per endpoint saying whether it read, what it returned,
    and which scope to add if it did not.

    One row per endpoint is deliberately more than the rate limit allows in a
    burst, so the probes are throttled rather than fired together.
    """
    semaphore = asyncio.Semaphore(4)

    async def probe(endpoint: Endpoint) -> dict:
        async with semaphore:
            row: dict = {
                "endpoint": endpoint.key,
                "name": endpoint.name,
                "path": endpoint.path,
                "scope": endpoint.scope,
                "verified_in_catalogue": endpoint.verified,
            }
            try:
                # One row is enough to prove the path and the response key; a
                # full sweep of forty endpoints would exhaust the minute.
                sample = await zoho.list_rows(endpoint, limit=1)
            except ZohoScopeError as exc:
                return row | {"ok": False, "reason": str(exc), "needs_scope": exc.scope}
            except ZohoError as exc:
                return row | {"ok": False, "reason": str(exc), "needs_scope": None}
            return row | {"ok": True, "sample_rows": len(sample), "needs_scope": None}

    return list(await asyncio.gather(*(probe(e) for e in SWEEPABLE)))
