"""Quotes, read out of Zoho Books.

Open to every signed-in user, like the leave calendar — not gated by team
grants. Note what that means: quote totals, customer names and contact details
are visible to anyone with a login. Narrowing it later is a small change (swap
``CurrentUser`` for a ``require_module`` dependency, as ``app.proposals.router``
does), and this comment is here so that decision stays a decision rather than an
oversight.

Nothing here writes. There is no POST, PUT or DELETE, and the Zoho token is
scoped to reads.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.auth.deps import CurrentUser
from app.core.config import get_settings
from app.zoho import service
from app.zoho.cache import QuoteCache
from app.zoho.client import ZohoBooks, ZohoError, ZohoRateLimitError
from app.zoho.schemas import QuoteDetailOut, QuoteListOut, QuoteOut, RelatedOut

router = APIRouter(prefix="/quotes", tags=["quotes"])

#: Zoho's own vocabulary, so a caller can pass what they see in the Books UI.
_STATUSES = ("draft", "sent", "invoiced", "accepted", "declined", "expired")


def get_zoho(request: Request) -> ZohoBooks:
    return request.app.state.zoho


def get_cache(request: Request) -> QuoteCache:
    return request.app.state.quote_cache


Zoho = Annotated[ZohoBooks, Depends(get_zoho)]
Cache = Annotated[QuoteCache, Depends(get_cache)]


def _safe_name(name: str | None) -> str:
    """A filename that cannot break out of the Content-Disposition header.

    The value comes from whatever somebody uploaded to Zoho, so a quote or a
    newline in it would let the caller write arbitrary response headers.
    """
    banned = {'"', "\\"}
    cleaned = "".join(
        c for c in (name or "attachment") if c.isprintable() and c not in banned
    )
    return cleaned.strip() or "attachment"


def _translate(exc: ZohoError) -> HTTPException:
    """Upstream failures, told apart so a client can react correctly."""
    if isinstance(exc, ZohoRateLimitError):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        # 503, not 502: this is temporary and worth retrying.
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers=headers,
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.get("", response_model=QuoteListOut, summary="All quotes")
async def list_quotes(
    _: CurrentUser,
    zoho: Zoho,
    cache: Cache,
    quote_status: Annotated[
        str | None, Query(alias="status", description=f"One of: {', '.join(_STATUSES)}")
    ] = None,
    customer_name: Annotated[str | None, Query()] = None,
    date_start: Annotated[str | None, Query(description="YYYY-MM-DD, inclusive")] = None,
    date_end: Annotated[str | None, Query(description="YYYY-MM-DD, inclusive")] = None,
    search: Annotated[str | None, Query(description="Free text across the quote")] = None,
    #: Omitted means every quote. Filters are conveniences here, not a way of
    #: keeping the response small — asking for all of them is the normal case.
    limit: Annotated[
        int | None, Query(ge=1, le=5000, description="Omit for every quote")
    ] = None,
    refresh: Annotated[bool, Query(description="Re-read Zoho instead of the cache")] = False,
) -> QuoteListOut:
    if quote_status is not None and quote_status.casefold() not in _STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"status must be one of: {', '.join(_STATUSES)}",
        )

    try:
        rows, truncated = await cache.quotes(
            zoho,
            refresh=refresh,
            status=quote_status.casefold() if quote_status else None,
            customer_name=customer_name,
            date_start=date_start,
            date_end=date_end,
            search_text=search,
            limit=limit,
        )
    except ZohoError as exc:
        raise _translate(exc) from exc

    app_base = get_settings().zoho_app_base
    return QuoteListOut(
        total=len(rows),
        truncated=truncated,
        quotes=[QuoteOut.from_zoho(r, app_base=app_base) for r in rows],
    )


@router.get(
    "/by-number/{number}",
    response_model=QuoteDetailOut,
    summary="Find a quote by its number",
)
async def quote_by_number(number: str, _: CurrentUser, zoho: Zoho) -> QuoteDetailOut:
    """Resolve a quote number into the quote itself.

    This is the bridge from the SharePoint Proposals list, whose ``quote_no``
    column holds exactly this string and nothing else.
    """
    try:
        matches = await zoho.estimates(estimate_number=number, limit=5)
        exact = next(
            (m for m in matches if (m.get("estimate_number") or "").strip() == number.strip()),
            None,
        )
        if exact is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No quote numbered {number!r} in Zoho Books",
            )
        return QuoteDetailOut.from_zoho(
            await zoho.estimate(exact["estimate_id"]), app_base=get_settings().zoho_app_base
        )
    except ZohoError as exc:
        raise _translate(exc) from exc


@router.get("/{quote_id}", response_model=QuoteDetailOut, summary="One quote in full")
async def quote_detail(quote_id: str, _: CurrentUser, zoho: Zoho) -> QuoteDetailOut:
    try:
        return QuoteDetailOut.from_zoho(
            await zoho.estimate(quote_id), app_base=get_settings().zoho_app_base
        )
    except ZohoError as exc:
        raise _translate(exc) from exc


@router.get(
    "/{quote_id}/documents/{document_id}",
    summary="Download an attachment on a quote",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def quote_document(
    quote_id: str, document_id: str, _: CurrentUser, zoho: Zoho
) -> Response:
    """Relay one attachment's bytes under the caller's own session.

    A relay rather than a redirect because Zoho authenticates with an OAuth
    header, and a browser following a link cannot send one. Handing the token to
    the browser instead would leak a credential that reads the whole
    organisation's Books data, so the bytes come through here.

    The document must belong to this quote. Zoho enforces that too — a foreign
    id is a 400 there — but it is checked here so a wrong id reads as "not
    found" rather than as an upstream failure, and so the rule does not depend
    on Zoho continuing to apply it.
    """
    try:
        detail = QuoteDetailOut.from_zoho(await zoho.estimate(quote_id))
        known = next((d for d in detail.documents if d.id == document_id), None)
        if known is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Quote {detail.number} has no attachment {document_id!r}",
            )

        content, content_type = await zoho.document(quote_id, document_id)
    except ZohoError as exc:
        raise _translate(exc) from exc

    return Response(
        content=content,
        media_type=content_type,
        headers={
            # inline so a PDF opens in the browser rather than forcing a save.
            # The quoted filename survives spaces, which these have.
            "Content-Disposition": f'inline; filename="{_safe_name(known.file_name)}"',
            "Content-Length": str(len(content)),
        },
    )


@router.get(
    "/{quote_id}/related",
    response_model=RelatedOut,
    summary="The records connected to a quote",
)
async def quote_related(
    quote_id: str,
    _: CurrentUser,
    zoho: Zoho,
    include: Annotated[
        str | None,
        Query(description=f"Comma-separated: {', '.join(service.BRANCHES)}. Omit for all."),
    ] = None,
) -> RelatedOut:
    """Customer, catalogue items, sales orders, invoices and comments.

    Each branch costs upstream calls, so ask only for what a screen shows. Any
    branch may come back ``ok: false`` with a reason while the others succeed —
    invoices currently always do, because this token's scope cannot read them.
    """
    try:
        detail = QuoteDetailOut.from_zoho(
            await zoho.estimate(quote_id), app_base=get_settings().zoho_app_base
        )
    except ZohoError as exc:
        raise _translate(exc) from exc

    return await service.related(zoho, detail, service.parse_include(include))
