"""The finance module: profit and loss, and the Zoho data behind it.

**Who may open this.** Super admin, CEO, Manager and Accountant — the four roles
in ``FINANCE_ROLES``. Unlike quotes, this is not open to every signed-in user,
and the difference is deliberate: a quote total is commercial information a
salesperson needs, whereas company profit, payroll-bearing expense accounts and
supplier margins are not. The guard is a global role rather than a team grant
because a P&L is not a team's data — there is one company statement and either
you may see it or you may not.

**Nothing here writes.** Every route is a GET, the client underneath issues only
GETs, and ``app.zoho.catalogue`` has no column that could describe a write. Zoho
Books stays the system of record; this module reports on it.

**Every figure is traceable.** The statement carries an ``integrity`` block
saying what could not be read and what did not reconcile, and any line can be
drilled into the documents behind it. A computed P&L that cannot be checked
against the books is not worth showing to a CEO.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.config import get_settings
from app.finance import service
from app.finance.accounts import Section
from app.finance.cache import PnlCache
from app.finance.pnl import by_month, drill, to_money
from app.finance.schemas import (
    ComparisonOut,
    DiagnosticsOut,
    DrilldownOut,
    EndpointStatusOut,
    MovementOut,
    PeriodOut,
    PostingOut,
    StatementOut,
    TrendOut,
    TrendPointOut,
)
from app.finance.service import Period
from app.roles.catalogue import FINANCE_ROLES
from app.roles.deps import require_roles
from app.zoho.catalogue import ALL_SCOPES, BY_KEY, LEDGER_SCOPES, SWEEPABLE
from app.zoho.client import ZohoBooks, ZohoError, ZohoRateLimitError

router = APIRouter(prefix="/finance", tags=["finance"])

#: Applied to every route in this module. Named once so a new route cannot be
#: added without it — the failure mode being silent, and the data being the
#: company's accounts.
FinanceUser = Annotated[object, Depends(require_roles(*FINANCE_ROLES))]


def get_zoho(request: Request) -> ZohoBooks:
    return request.app.state.zoho


def get_cache(request: Request) -> PnlCache:
    return request.app.state.pnl_cache


Zoho = Annotated[ZohoBooks, Depends(get_zoho)]
Cache = Annotated[PnlCache, Depends(get_cache)]


def _translate(exc: ZohoError) -> HTTPException:
    """Upstream failures, told apart so a client can react correctly."""
    if isinstance(exc, ZohoRateLimitError):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers=headers,
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


def _period(
    preset: str | None, start: date | None, end: date | None
) -> Period:
    try:
        return service.resolve(preset, start, end)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


# Shared query parameters, declared once so the routes agree on their meaning.
Preset = Annotated[
    str | None,
    Query(description=f"One of: {', '.join(service.PRESETS)}. Ignored if dates are given."),
]
Start = Annotated[date | None, Query(description="YYYY-MM-DD, inclusive")]
End = Annotated[date | None, Query(description="YYYY-MM-DD, inclusive")]
Refresh = Annotated[bool, Query(description="Re-read Zoho instead of the cache")]


async def _statement(
    zoho: ZohoBooks, cache: PnlCache, period: Period, *, refresh: bool
):
    """One period's statement and the postings behind it."""
    swept = await cache.sweep(zoho, period, refresh=refresh)
    currency = await service.base_currency(zoho)
    return service.statement_from(swept, period, currency)


@router.get(
    "/profit-and-loss",
    response_model=StatementOut,
    summary="Profit and loss for a period",
)
async def profit_and_loss(
    _: FinanceUser,
    zoho: Zoho,
    cache: Cache,
    period: Preset = None,
    start: Start = None,
    end: End = None,
    refresh: Refresh = False,
) -> StatementOut:
    """The statement, computed from the ledger.

    Read ``integrity`` before trusting the totals. ``complete: false`` means a
    source could not be read and the figures are a floor, not the answer.
    """
    window = _period(period, start, end)
    try:
        statement, _walk = await _statement(zoho, cache, window, refresh=refresh)
    except ZohoError as exc:
        raise _translate(exc) from exc
    return StatementOut.of(statement, window)


@router.get(
    "/profit-and-loss/comparison",
    response_model=ComparisonOut,
    summary="A period against the one before it",
)
async def comparison(
    _: FinanceUser,
    zoho: Zoho,
    cache: Cache,
    period: Preset = None,
    start: Start = None,
    end: End = None,
    refresh: Refresh = False,
) -> ComparisonOut:
    """This period beside the preceding one of equal length.

    Equal length rather than the previous calendar month — see
    ``Period.previous`` for why comparing 31 days against 28 and calling the
    difference growth is a trap worth avoiding by construction.
    """
    window = _period(period, start, end)
    prior = window.previous()

    try:
        current, _ = await _statement(zoho, cache, window, refresh=refresh)
        previous, _ = await _statement(zoho, cache, prior, refresh=refresh)
    except ZohoError as exc:
        raise _translate(exc) from exc

    movements = [
        _movement(key, label, getattr(current, attr), getattr(previous, attr))
        for key, label, attr in (
            ("income", "Operating income", "income"),
            ("cost_of_sales", "Cost of sales", "cost_of_sales"),
            ("gross_profit", "Gross profit", "gross_profit"),
            ("operating_expenses", "Operating expenses", "operating_expenses"),
            ("operating_profit", "Operating profit", "operating_profit"),
            ("net_profit", "Net profit", "net_profit"),
        )
    ]

    return ComparisonOut(
        current=StatementOut.of(current, window),
        previous=StatementOut.of(previous, prior),
        movements=movements,
    )


def _movement(key: str, label: str, current, previous) -> MovementOut:
    change = current - previous
    return MovementOut(
        key=key,
        label=label,
        current=float(to_money(current)),
        previous=float(to_money(previous)),
        change=float(to_money(change)),
        # Guarded rather than defaulted: a move from zero has no percentage,
        # and both 100% and infinity would be inventions.
        change_percent=(
            float(to_money(change / abs(previous) * 100)) if previous else None
        ),
    )


@router.get(
    "/profit-and-loss/trend",
    response_model=TrendOut,
    summary="The same period cut by month",
)
async def trend(
    _: FinanceUser,
    zoho: Zoho,
    cache: Cache,
    period: Preset = "last_12_months",
    start: Start = None,
    end: End = None,
    refresh: Refresh = False,
) -> TrendOut:
    """A monthly series, built from one sweep rather than twelve.

    Defaults to twelve months because a single month has no trend to show.
    """
    window = _period(period, start, end)
    try:
        statement, walk = await _statement(zoho, cache, window, refresh=refresh)
    except ZohoError as exc:
        raise _translate(exc) from exc

    points = [
        TrendPointOut(
            month=month,
            income=float(to_money(totals[Section.INCOME] + totals[Section.OTHER_INCOME])),
            cost_of_sales=float(to_money(totals[Section.COST_OF_SALES])),
            operating_expenses=float(to_money(totals[Section.OPERATING_EXPENSES])),
            net_profit=float(
                to_money(
                    totals[Section.INCOME]
                    + totals[Section.OTHER_INCOME]
                    - totals[Section.COST_OF_SALES]
                    - totals[Section.OPERATING_EXPENSES]
                    - totals[Section.OTHER_EXPENSES]
                )
            ),
        )
        for month, totals in by_month(walk).items()
    ]

    return TrendOut(
        period=PeriodOut.of(window), currency=statement.currency, points=points
    )


@router.get(
    "/accounts/{account_id}/postings",
    response_model=DrilldownOut,
    summary="The documents behind one account's figure",
)
async def account_postings(
    account_id: str,
    _: FinanceUser,
    zoho: Zoho,
    cache: Cache,
    period: Preset = None,
    start: Start = None,
    end: End = None,
) -> DrilldownOut:
    """Every posting that made up one line of the statement.

    Served from the same cached sweep as the statement, so opening a figure
    costs nothing upstream and cannot disagree with the total it came from.
    """
    window = _period(period, start, end)
    try:
        statement, walk = await _statement(zoho, cache, window, refresh=False)
    except ZohoError as exc:
        raise _translate(exc) from exc

    account = next(
        (
            line
            for total in statement.sections.values()
            for line in total.accounts
            if line.account_id == account_id
        ),
        None,
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No account {account_id!r} contributed to the profit and loss for "
                f"{window.label}. It may exist but have no postings in this period."
            ),
        )

    return DrilldownOut(
        period=PeriodOut.of(window),
        account_id=account.account_id,
        account_name=account.name,
        section=account.section.value,
        total=float(to_money(account.amount)),
        postings=[PostingOut.of(p) for p in drill(walk, account_id)],
    )


@router.get(
    "/diagnostics",
    response_model=DiagnosticsOut,
    summary="What this Zoho token can actually read",
)
async def zoho_diagnostics(_: FinanceUser, zoho: Zoho) -> DiagnosticsOut:
    """Probe every endpoint and report which are readable under the current token.

    Deliberately an endpoint rather than a script. The answer changes whenever
    somebody regenerates the refresh token, and the person who needs it —
    whoever is being asked to widen the scope — should be able to see it without
    a deployment.

    One upstream call per endpoint, throttled. Not something to poll.
    """
    if not zoho.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Zoho Books is not configured: set ZOHO_CLIENT_ID, "
                "ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN and ZOHO_ORGANIZATION_ID."
            ),
        )

    rows = await service.diagnostics(zoho)
    endpoints = [EndpointStatusOut(**row) for row in rows]

    return DiagnosticsOut(
        configured=True,
        organization_id=get_settings().zoho_organization_id,
        all_scopes=list(ALL_SCOPES),
        ledger_scopes=list(LEDGER_SCOPES),
        missing_scopes=sorted({e.needs_scope for e in endpoints if e.needs_scope}),
        readable=sum(1 for e in endpoints if e.ok),
        total=len(endpoints),
        endpoints=endpoints,
    )


@router.get("/zoho", summary="Every Zoho endpoint this module can read")
async def zoho_endpoints(_: FinanceUser) -> dict:
    """The catalogue itself, without calling Zoho.

    What ``/finance/zoho/{endpoint}`` accepts, and which scope each needs. Free
    to call — this is a description of the code, not a read of the accounts.
    """
    return {
        "endpoints": [
            {
                "key": e.key,
                "name": e.name,
                "path": e.path,
                "scope": e.scope,
                "verified": e.verified,
                "ledger": e.ledger,
            }
            for e in SWEEPABLE
        ],
        "scopes": list(ALL_SCOPES),
    }


@router.get("/zoho/{endpoint_key}", summary="Read one Zoho endpoint directly")
async def zoho_passthrough(
    endpoint_key: str,
    _: FinanceUser,
    zoho: Zoho,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    date_start: Annotated[str | None, Query(description="YYYY-MM-DD")] = None,
    date_end: Annotated[str | None, Query(description="YYYY-MM-DD")] = None,
) -> dict:
    """Rows from any catalogued endpoint, unmodified.

    The escape hatch for the reporting the statement does not cover — ageing,
    supplier analysis, a figure somebody wants checked — without a code change
    per question. Rows come back exactly as Zoho sent them: this is the one place
    in the module that does not translate Zoho's vocabulary, because the point of
    it is to see what Zoho actually holds.

    ``limit`` defaults to 200 rather than to everything. An unbounded sweep of a
    large module is several seconds and a real slice of the rate limit, and a
    caller exploring the data rarely wants all of it.
    """
    endpoint = BY_KEY.get(endpoint_key)
    if endpoint is None or endpoint.parameterised:
        known = ", ".join(e.key for e in SWEEPABLE)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No readable endpoint {endpoint_key!r}. One of: {known}",
        )

    params = {"date_start": date_start, "date_end": date_end}
    try:
        rows = await zoho.list_rows(
            endpoint, params={k: v for k, v in params.items() if v}, limit=limit
        )
    except ZohoError as exc:
        raise _translate(exc) from exc

    return {
        "endpoint": endpoint.key,
        "scope": endpoint.scope,
        "count": len(rows),
        # True when the sweep stopped at the limit, so a caller can tell "200
        # rows" from "200 rows and there were more".
        "truncated": len(rows) >= limit,
        "rows": rows,
    }
