"""Quote requests: raise one, get it approved, join the queue.

The flow, in the order presales actually works:

1. ``POST /quote-requests`` — the form. Zoho-shaped fields, so the integration
   later is a mapping rather than a rewrite.
2. ``POST /{id}/supplier-quotes`` — upload what the suppliers sent. Read,
   compared and attached, reusing the comparison module rather than a second
   implementation of the same thing.
3. ``POST /{id}/select-supplier`` — take one of them as the quote's own priced
   lines. Their unit price is the cost, the selling rate is that plus a margin,
   and the lines are editable afterwards like any others. **Until this happens
   the quote has nothing of its own to approve**, which is why it is also what
   turns ``may_submit`` on.
4. ``POST /{id}/submit`` — to the approvers. The request locks: editing it while
   they are looking would mean they approved something that no longer exists.
5. ``POST /{id}/reviews`` — approve, reject, send back for rework, or just
   comment. Approving may name a different supplier, which reprices the lines.
   Rework returns it to the requester and the loop goes round again.
6. ``GET /queue`` — approved and waiting to be created in Zoho.

Beside the flow, the **selling & costing report**: ``GET /{id}/report`` is
what an approver reads — price, landed cost, margin, walk-away and the
discount ladder — computed on read from the quote, and ``GET /{id}/report.pdf``
is the same thing as a page to attach or print. Submitting mails it to the
approvers. See ``app/quoting/report.py``.

**Nothing here touches Zoho.** The list is live; the push is a later piece of
work and the queue is deliberately where this stops. The only Zoho traffic is a
read of past estimates to work out a win probability.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import service as access_service
from app.auth.deps import CurrentUser
from app.comparison import service as comparison_service
from app.comparison.documents import DocumentError, prepare
from app.comparison.extraction import ExtractionError, QuoteExtractor
from app.comparison.router import _dec, _txt
from app.comparison.schemas import ComparisonIn
from app.comparison.schemas import ItemIn as SupplierItemIn
from app.comparison.schemas import QuoteIn as SupplierQuoteIn
from app.core.config import get_settings
from app.core.db import get_session
from app.models.comparison import QuoteSource
from app.models.quoting import DocumentKind, QuoteRequest, QuoteStatus
from app.proposals import mirror
from app.proposals.router import get_sharepoint
from app.proposals.sharepoint import SharePointError, SharePointProposals
from app.quoting import bidpack, filing, report_pdf, service
from app.quoting import calculation as calc
from app.quoting import report as report_mod
from app.quoting import workbook as workbook_mod
from app.quoting.fx import FxUnavailableError, zoho_rate
from app.quoting.mailer import QuoteMailer
from app.quoting.probability import WinRates
from app.quoting.schemas import (
    ApplySuggestionsIn,
    BidPackOut,
    CalcStepOut,
    CommentIn,
    CommentOut,
    CostingReportOut,
    CurrencyIn,
    FxQuoteOut,
    ItemOut,
    NegotiationIn,
    QuotableTaskOut,
    QuotableTasksOut,
    QuoteDocumentOut,
    QuoteRequestIn,
    QuoteRequestOut,
    QuoteSummaryOut,
    ReviewIn,
    ReviewOut,
    SupplierChoiceIn,
    TaskQuoteIn,
    TypedSupplierQuotesIn,
)
from app.quoting.service import QuoteError, QuoteNotFoundError, QuotePermissionError
from app.quoting.storage import DriveError, QuoteDrive
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles
from app.teams import service as teams_service
from app.teams.service import TeamError
from app.zoho.client import ZohoBooks, ZohoError

MODULE_KEY = "quote_requests"

Session = Annotated[AsyncSession, Depends(get_session)]


async def require_module(
    user: CurrentUser, roles: CurrentRoles, session: Session
) -> None:
    """The caller's team must have been granted the quote requests module.

    These are live customer prices *and* the cost behind them, so the margin on
    every job is readable by anyone who can reach this module. That is a narrower
    audience than "signed in", which is what it was.
    """
    if not await access_service.can_reach(
        session, user_id=user.id, global_roles=roles, module_key=MODULE_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your team does not have the Quote Requests module. "
                "A super admin can grant it."
            ),
        )


#: On the router rather than on each route. A gate that has to be remembered
#: every time a route is added is a gate that will eventually be missed, and the
#: route that misses it will be the one nobody thinks to check.
logger = logging.getLogger("hamdaz.quoting")

router = APIRouter(
    prefix="/quote-requests",
    tags=["quote requests"],
    dependencies=[Depends(require_module)],
)

#: Each upload is a document read by a model, against a real bill.
MAX_UPLOADS = 12


def get_extractor(request: Request) -> QuoteExtractor:
    return request.app.state.quote_extractor


def get_zoho(request: Request) -> ZohoBooks:
    return request.app.state.zoho


def get_win_rates(request: Request) -> WinRates:
    return request.app.state.win_rates


def get_mailer(request: Request) -> QuoteMailer:
    return request.app.state.quote_mailer


def get_text_model(request: Request):
    """The optional second-pass model. Absent on a test app, which is the
    same as configured with no keys: the readers stand alone."""
    return getattr(request.app.state, "text_model", None)


def get_drive(request: Request) -> QuoteDrive:
    return request.app.state.quote_drive


Extractor = Annotated[QuoteExtractor, Depends(get_extractor)]
Zoho = Annotated[ZohoBooks, Depends(get_zoho)]
Rates = Annotated[WinRates, Depends(get_win_rates)]
SharePoint = Annotated[SharePointProposals, Depends(get_sharepoint)]
Mailer = Annotated[QuoteMailer, Depends(get_mailer)]
Drive = Annotated[QuoteDrive, Depends(get_drive)]
Model = Annotated[Any, Depends(get_text_model)]


def _translate(exc: QuoteError) -> HTTPException:
    if isinstance(exc, QuoteNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, QuotePermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def _out(
    session: AsyncSession, request: QuoteRequest, *, user, roles: set[str]
) -> QuoteRequestOut:
    body = QuoteRequestOut.model_validate(request)
    body.team_name = request.team.name if request.team else None
    body.created_by_name = request.created_by.display_name if request.created_by else None
    body.assigned_to_name = request.assigned_to.display_name if request.assigned_to else None
    for out, row in zip(body.reviews, request.reviews, strict=True):
        out.reviewer_name = row.reviewer.display_name if row.reviewer else None
    for out, row in zip(body.comments, request.comments, strict=True):
        out.author_name = row.author.display_name if row.author else None

    # Everything derived — the landed cost, the margin ladder, what the buyer
    # will read our price against, what is still outstanding. Computed here on
    # every read rather than stored, so it can never disagree with the inputs
    # it came from. See ``app.quoting.bidpack``.
    # Where each line's price came from, so the editor can rebuild a price the
    # way it was built — in the supplier's currency first — when the margin is
    # retyped, rather than marking up a converted cost and drifting a cent.
    sources = service.supplier_prices(request)
    for out in body.items:
        found = sources.get(str(out.id))
        if found is not None:
            out.supplier_unit_price, out.supplier_currency = found

    pack = bidpack.build(request)
    body.bid = BidPackOut.model_validate(pack, from_attributes=True)
    # And the working behind every figure, from the same pass.
    body.calculation = [
        CalcStepOut.model_validate(step, from_attributes=True)
        for step in calc.steps(request, pack)
    ]

    body.documents = _documents_of(request)

    # Whoever raised it, or a super admin — while it is in a state that can be
    # edited at all. Submitting still freezes it for everyone.
    body.may_edit = request.is_editable and (
        request.created_by_id == user.id or SUPER_ADMIN in set(roles)
    )
    body.submit_reason = service.why_not_submit(request)
    body.may_submit = body.may_edit and body.submit_reason is None
    allowed, reason = await service.may_approve(session, request, user=user, roles=roles)
    body.may_approve = allowed
    body.approve_reason = None if allowed else reason
    # Its own rule: whose quote it is, plus a super admin, and no status in
    # it at all. See ``service.may_set_currency``.
    body.may_set_currency = service.may_set_currency(request, user=user, roles=roles)
    # Asked here rather than re-derived on the screen, like every other
    # permission on this body. A frontend that works out for itself who may
    # delete something is a frontend that will one day disagree with the server.
    body.may_delete = SUPER_ADMIN in set(roles)
    return body


def _documents_of(request: QuoteRequest) -> list[QuoteDocumentOut]:
    """Every document on the quote, filed rows first.

    A supplier quotation attached before the documents table existed has no
    row of its own, so it is listed from its comparison row — openable, not
    deletable. Both relationships are selectin-loaded: no extra query, and no
    lazy load from inside async code.
    """
    suppliers = (
        {quote.id: quote for quote in request.comparison.quotes}
        if request.comparison is not None
        else {}
    )
    out: list[QuoteDocumentOut] = []
    seen: set[uuid.UUID] = set()
    for document in request.documents:
        row = QuoteDocumentOut(**filing.summary(document))
        if document.supplier_quote_id is not None:
            seen.add(document.supplier_quote_id)
            quote = suppliers.get(document.supplier_quote_id)
            row.supplier_name = quote.supplier_name if quote else None
            row.is_selected = document.supplier_quote_id == request.selected_supplier_quote_id
        out.append(row)
    for quote in suppliers.values():
        if quote.id in seen or not quote.file_name:
            continue
        out.append(
            QuoteDocumentOut(
                kind=str(DocumentKind.SUPPLIER_QUOTE),
                kind_label=filing.kind_label(DocumentKind.SUPPLIER_QUOTE),
                file_name=quote.file_name,
                content_type=quote.file_type,
                drive_url=quote.drive_url,
                supplier_quote_id=quote.id,
                supplier_name=quote.supplier_name,
                is_selected=quote.id == request.selected_supplier_quote_id,
            )
        )
    return out


def _summary(
    request: QuoteRequest, roles: set[str] | frozenset[str] = frozenset()
) -> QuoteSummaryOut:
    return QuoteSummaryOut(
        id=request.id,
        reference=request.reference,
        title=request.title,
        customer_name=request.customer_name,
        status=request.status,
        revision=request.revision,
        currency=request.currency,
        total=request.total,
        win_probability=request.win_probability,
        multiple_supplier_quotes=request.multiple_supplier_quotes,
        created_by_name=request.created_by.display_name if request.created_by else None,
        assigned_to_name=request.assigned_to.display_name if request.assigned_to else None,
        open_comments=sum(1 for c in request.comments if c.is_open),
        # What a list of bids is actually scanned for. A total says what one is
        # worth; this says whether it can be sent.
        blocking_issues=sum(1 for c in request.compliance if c.is_blocking),
        rfp_number=request.rfp_number,
        cf_bcd=request.cf_bcd,
        may_delete=SUPER_ADMIN in set(roles),
        created_at=request.created_at,
    )


async def _load(session: AsyncSession, request_id: uuid.UUID) -> QuoteRequest:
    try:
        return await service.get(session, request_id)
    except QuoteError as exc:
        raise _translate(exc) from exc


# ── 1. the form ────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=QuoteRequestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Raise a quote request",
)
async def create(
    payload: QuoteRequestIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    zoho: Zoho,
    rates: Rates,
    team: Annotated[str, Query(description="Team handle or id")],
) -> QuoteRequestOut:
    """The form, in Zoho's own field names.

    A win probability is worked out from the estimate history and frozen onto
    the record — the number only means anything beside the counts it came from,
    which is why ``win_basis`` travels with it.
    """
    return await _raise_quote(
        session, payload, user=user, roles=roles, team=team, zoho=zoho, rates=rates
    )


async def _raise_quote(
    session: AsyncSession,
    payload: QuoteRequestIn,
    *,
    user,
    roles: set[str],
    team: str,
    zoho: ZohoBooks,
    rates: WinRates,
    task=None,
) -> QuoteRequestOut:
    """Raise one, whether it was typed in or started from a task."""
    try:
        resolved = await teams_service.get_team(session, team)
    except TeamError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        request = await service.create(
            session, payload=payload.model_dump(), author=user, team=resolved
        )
    except QuoteError as exc:
        raise _translate(exc) from exc

    if task is not None:
        request.source_task_id = task.id
        request.source_task_url = task.web_url

    estimate = await rates.estimate(zoho, customer_name=payload.customer_name)
    request.win_probability = estimate.probability
    request.win_basis = estimate.basis
    await session.flush()
    return await _out(session, request, user=user, roles=roles)


@router.get(
    "/fx-rate",
    response_model=FxQuoteOut,
    summary="Zoho Books' exchange rate between two currencies",
)
async def fx_rate(
    _user: CurrentUser,
    zoho: Zoho,
    quote: Annotated[str, Query(min_length=3, max_length=3, description="The quote's currency")],
    supplier: Annotated[
        str, Query(min_length=3, max_length=3, description="The supplier's currency")
    ],
) -> FxQuoteOut:
    """One unit of the quote's currency in the supplier's — "1 USD = 3.672501
    AED" — as Zoho will convert the estimate at. Shown with its working, both
    sides against Zoho's base, so a person can see it is the ratio of two
    figures somebody set in Zoho and not a number this app made up."""
    try:
        found = await zoho_rate(zoho, quote_currency=quote, supplier_currency=supplier)
    except FxUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ZohoError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read Zoho Books' currency table.",
        ) from exc
    return FxQuoteOut.model_validate(found)


@router.get(
    "/tasks",
    response_model=QuotableTasksOut,
    summary="Your Proposals tasks, and which of them already have a quote",
)
async def quotable_tasks(
    user: CurrentUser,
    session: Session,
    sharepoint: SharePoint,
    scope: Annotated[
        Literal["live", "open", "all"],
        Query(
            description=(
                "live: not finished and the bid has not closed — what can still be "
                "quoted for. open: everything not finished, closed bids included. "
                "all: completed ones too."
            )
        ),
    ] = "live",
) -> QuotableTasksOut:
    """The caller's own work, as the starting point for a quote.

    **Live by default.** Most of what is assigned to anybody is a bid that
    closed months ago and was never marked finished; listing it first buried
    the handful that can still be quoted for. The other two scopes are one
    click away for the person who wants them.

    The same rows the Proposals page shows — every field of them, so the quote
    can be started from what is already written down rather than retyped — with
    the quote already raised against each one attached where there is one.

    Whose tasks these are comes from the session. There is no parameter here
    that can return somebody else's.

    Served by this module rather than read from the Proposals one, so raising a
    quote needs the quoting module and not also that one.

    **From the local copy of the list, not the list.** That is what makes the
    answer complete rather than merely quick. The list client pages and then
    truncates to its first few hundred rows in SharePoint's own order — oldest
    first — before any caller can sort, which removed precisely the live bids
    this screen exists to offer: a busy assignee saw "0 live" against ten real
    ones. The mirror is a full read of the list every sync and runs seconds
    behind it. See app/proposals/mirror.py.
    """
    try:
        lookup_id = await sharepoint.lookup_id_for(user.email)
    except SharePointError as exc:
        # The only thing here that still asks SharePoint anything, and so
        # the only thing that can fail this way.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read the Proposals list",
        ) from exc

    if lookup_id is None:
        # No presence on that SharePoint site, so nothing could be assigned to
        # them. Not an error, and told apart from having no tasks.
        return QuotableTasksOut(
            email=user.email,
            in_sharepoint=False,
            total=0,
            open_count=0,
            live_count=0,
            quoted_count=0,
            tasks=[],
        )

    tasks = await mirror.tasks_for(session, lookup_id)

    open_tasks = [t for t in tasks if t.is_open]
    live_tasks = [t for t in open_tasks if t.is_active]
    shown = {"live": live_tasks, "open": open_tasks, "all": tasks}[scope]
    # Soonest deadline first, by BCD rather than DueDate — the Proposals list
    # orders itself the same way, and for the same reason.
    shown = sorted(shown, key=lambda t: (t.deadline is None, t.deadline or ""))

    raised = await service.quotes_for_tasks(session, [str(t.id) for t in shown])
    rows = [QuotableTaskOut.of(task, raised.get(str(task.id))) for task in shown]
    return QuotableTasksOut(
        email=user.email,
        in_sharepoint=True,
        total=len(tasks),
        open_count=len(open_tasks),
        live_count=len(live_tasks),
        quoted_count=sum(1 for r in rows if r.quote_request_id is not None),
        tasks=rows,
    )


@router.post(
    "/from-task",
    response_model=QuoteRequestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Raise a quote for one of your Proposals tasks",
)
async def create_from_task(
    payload: TaskQuoteIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    zoho: Zoho,
    rates: Rates,
    sharepoint: SharePoint,
    team: Annotated[str, Query(description="Team handle or id")],
) -> QuoteRequestOut:
    """Start from the enquiry instead of retyping it.

    The task is looked up **among the caller's own tasks**, so there is no
    parameter here that can raise a quote against somebody else's work — the
    same rule the Proposals list itself follows.

    What the row knows is filled in: the title, the customer, the bid closing
    date Zoho calls ``cf_bcd``, its Zoho quote number, and the remarks. What it
    cannot know — the lines and their prices — is the reason for raising the
    quote and comes next, from the supplier quotes.
    """
    lookup_id = await sharepoint.lookup_id_for(user.email)
    tasks = await mirror.tasks_for(session, lookup_id) if lookup_id else []
    task = next((t for t in tasks if str(t.id) == payload.task_id), None)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That task is not one of yours, or is no longer on the Proposals list.",
        )

    form = QuoteRequestIn(**service.payload_from_task(task))
    return await _raise_quote(
        session, form, user=user, roles=roles, team=team, zoho=zoho, rates=rates, task=task
    )


@router.patch("/{request_id}", response_model=QuoteRequestOut, summary="Edit a quote")
async def update(
    request_id: uuid.UUID,
    payload: QuoteRequestIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
) -> QuoteRequestOut:
    """Only while it is yours — in draft, or after a rework.

    The body is the whole quote, lists included: the lines, the landed-cost
    build-up, the compliance matrix and the portal checklist are each replaced
    wholesale rather than merged. A partial update would need the caller to
    track ids that only exist after a save, and the failure mode — a list
    quietly emptied by a body that simply did not mention it — is the one people
    notice last. The compliance and portal rows send their ids back so that when
    a gap was closed, and when a cell was typed, survive the replace.
    """
    request = await _load(session, request_id)
    try:
        service.require_editable(request, user=user, roles=roles)
        service.apply_fields(request, payload.model_dump())
        service.set_items(request, [i.model_dump() for i in payload.items])
        service.set_cost_lines(request, [c.model_dump() for c in payload.cost_lines])
        service.set_compliance(request, [c.model_dump() for c in payload.compliance])
        service.set_submission_fields(
            request, [f.model_dump() for f in payload.submission_fields]
        )
        await session.flush()
    except QuoteError as exc:
        raise _translate(exc) from exc
    return await _out(session, request, user=user, roles=roles)


@router.patch(
    "/{request_id}/currency",
    response_model=QuoteRequestOut,
    summary="Set the currency, in any state the quote is in",
)
async def set_currency(
    request_id: uuid.UUID,
    payload: CurrencyIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    zoho: Zoho,
) -> QuoteRequestOut:
    """Switch the currency, converting every figure at Zoho's rate.

    A quote in the wrong currency is not fixed by relabelling it: 4,802.40 does
    not stop being dirhams because the dropdown says USD. So the switch
    converts — lines, discounts, shipping, the landed-cost rows, the
    submission figures — at the rate Zoho Books will convert the estimate at,
    and rounds to the cent, once. Tax rates, quantities and margins are not
    money and do not move.

    Allowed in any state, by whoever raised or holds the quote and by a super
    admin, because a quote discovered to be in the wrong currency should not
    need pulling out of approval to correct.

    Whose quote it is still decides, as with every other write — plus a super
    admin, who holds every access here and should not have to ask the author to
    correct a label. An approver who wants another currency asks the person who
    raised it, the same as with any other correction.
    """
    request = await _load(session, request_id)
    if not service.may_set_currency(request, user=user, roles=roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This quote is not yours to set the currency on.",
        )
    try:
        await service.convert_currency(
            session,
            request,
            to_currency=payload.currency,
            rates=lambda ours, theirs: zoho_rate(
                zoho, quote_currency=ours, supplier_currency=theirs
            ),
        )
    except QuoteError as exc:
        raise _translate(exc) from exc
    except ZohoError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read Zoho Books' currency table to convert the quote.",
        ) from exc
    return await _out(session, request, user=user, roles=roles)


@router.get(
    "/{request_id}/workbook",
    summary="The bid pack as an Excel workbook",
    response_class=Response,
    responses={200: {"content": {workbook_mod.XLSX_TYPE: {}}}},
)
async def download_workbook(
    request_id: uuid.UUID,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
) -> Response:
    """The five sheets presales already works in, built from the stored bid.

    A rendering, not a second source of truth: every figure comes from the same
    computation the screen draws, so the workbook and the page it came from
    cannot disagree.

    Readable by anyone who can open the quote — which the module gate and the
    listing rules have already settled — because a bid is worked on with people
    who are not going to sign in to look at it.
    """
    request = await _load(session, request_id)
    content = workbook_mod.build(request)
    name = workbook_mod.filename_for(request)
    logger.info("workbook for quote %s downloaded by %s", request_id, user.email)
    return Response(
        content=content,
        media_type=workbook_mod.XLSX_TYPE,
        headers={
            # Quoted, because the name carries spaces on a bid whose event
            # number has them.
            "Content-Disposition": f'attachment; filename="{name}"',
        },
    )


async def _base_rate(request: QuoteRequest, zoho: ZohoBooks) -> tuple[Any, str | None]:
    """One unit of the quote's currency in AED, for the report's second column.

    The quote's own rate when its supplier is in AED — that is the same
    number, and the one the estimate will be converted at. Otherwise Zoho's,
    read now. Neither is stored: a rate is an input to a rendering, and the
    report says which one it used. When nothing can supply one the report is
    in the quote's currency alone, and says that too.
    """
    ours = (request.currency or report_mod.BASE_CURRENCY).upper()
    if ours == report_mod.BASE_CURRENCY:
        return None, None
    theirs = (request.supplier_currency or "").upper()
    if theirs == report_mod.BASE_CURRENCY and request.fx_rate and request.fx_rate > 0:
        return request.fx_rate, "the quote's own rate"
    try:
        found = await zoho_rate(
            zoho, quote_currency=ours, supplier_currency=report_mod.BASE_CURRENCY
        )
    except (FxUnavailableError, ZohoError) as exc:
        logger.info("no AED rate for the report on quote %s: %s", request.id, exc)
        return None, None
    except Exception as exc:  # noqa: BLE001 - a report without a rate beats no report
        logger.warning("Zoho rate lookup failed for quote %s: %s", request.id, exc)
        return None, None
    return found.rate, "Zoho Books"


async def _report(request: QuoteRequest, zoho: ZohoBooks):
    rate, source = await _base_rate(request, zoho)
    return report_mod.build(request, base_rate=rate, rate_source=source)


@router.get(
    "/{request_id}/report",
    response_model=CostingReportOut,
    summary="The selling & costing report, as figures",
)
async def costing_report(
    request_id: uuid.UUID,
    _: CurrentUser,
    session: Session,
    zoho: Zoho,
) -> CostingReportOut:
    """What an approver reads: the quoted price against the landed cost, the
    margin that leaves, the walk-away price and what each discount step does
    to the margin. Computed from the quote on every read — nothing here is
    stored, so it cannot disagree with the quote it is about.
    """
    request = await _load(session, request_id)
    return CostingReportOut.model_validate(await _report(request, zoho))


@router.get(
    "/{request_id}/report.pdf",
    summary="The selling & costing report as a PDF",
    response_class=Response,
    responses={200: {"content": {report_pdf.PDF_TYPE: {}}}},
)
async def costing_report_pdf(
    request_id: uuid.UUID,
    user: CurrentUser,
    session: Session,
    zoho: Zoho,
) -> Response:
    """The same report, as the page it is printed on and mailed as.

    A rendering of the figures the JSON route returns, never a second
    computation of them.
    """
    request = await _load(session, request_id)
    content = report_pdf.render(await _report(request, zoho))
    name = report_pdf.filename_for(request)
    logger.info("costing report for quote %s downloaded by %s", request_id, user.email)
    return Response(
        content=content,
        media_type=report_pdf.PDF_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@router.delete(
    "/{request_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a quote request (super admin)",
)
async def destroy(
    request_id: uuid.UUID,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
) -> None:
    """Remove a quote request and everything hanging off it. Super admin only.

    Not the author's to do, on purpose. A quote carries an appended approval
    history whose whole value is that it cannot be rewritten, and a delete in
    the author's hands would be a rewrite with an extra step.

    Answers 204 with no body. There is nothing left to return, and a caller
    that wants the list refreshes it.
    """
    request = await _load(session, request_id)
    try:
        await service.delete_request(session, request, roles=roles)
    except QuoteError as exc:
        raise _translate(exc) from exc
    logger.info("quote %s deleted by %s", request_id, user.email)


# ── 2. the supplier quotes behind it ───────────────────────────────────


@router.post(
    "/{request_id}/supplier-quotes",
    response_model=QuoteRequestOut,
    summary="Attach supplier quotes and compare them",
)
async def attach_suppliers(
    request_id: uuid.UUID,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    extractor: Extractor,
    drive: Drive,
    zoho: Zoho,
    files: Annotated[
        list[UploadFile] | None,
        File(description="Supplier quotes: PDF, image, XLSX, CSV or DOCX"),
    ] = None,
    currency: Annotated[
        str | None,
        Form(
            description="The currency these offers are in. Overrides what the reader "
            "makes of the document, for one that does not say or says it badly."
        ),
    ] = None,
) -> QuoteRequestOut:
    """Upload what the suppliers sent.

    ``currency`` is for the document that never names its currency, or names
    it somewhere the reader does not look: a dollar quotation read as nothing
    used to be labelled in the quote's own currency and priced as dirhams.
    Given, it wins over whatever the reader found, and the quote follows it.

    Each document is read by the comparison module's own reader — no model
    behind it, so a regular quote costs nothing and a scan is declined with a
    message saying to type it in (``/supplier-quotes/typed``). The comparison
    is computed and attached; which supplier wins is an approver's decision,
    not this endpoint's.
    """
    request = await _load(session, request_id)
    try:
        service.require_editable(request, user=user)
    except QuoteError as exc:
        raise _translate(exc) from exc

    uploads = files or []
    told = (currency or "").strip().upper()[:3] or None
    if told is not None and not told.isalpha():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{currency!r} is not a currency code.",
        )
    if len(uploads) > MAX_UPLOADS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_UPLOADS} supplier quotes at a time",
        )

    readables, failures = [], []
    # The bytes exactly as they arrived, kept beside the prepared versions.
    # `prepare` converts a DOCX to text and leaves `Readable.data` empty, so the
    # original is the only thing worth filing — a colleague opening the drive
    # wants the supplier's own document, not our extraction of it.
    originals: dict[str, tuple[bytes, str | None]] = {}
    for upload in uploads:
        name = upload.filename or "unnamed"
        try:
            raw = await upload.read()
            # In a thread: OCR on a scan can take a minute, and the server
            # has other requests to answer meanwhile.
            readables.append(await asyncio.to_thread(prepare, name, raw, upload.content_type))
            originals[name] = (raw, upload.content_type)
        except DocumentError as exc:
            failures.append(f"{name}: {exc}")

    quotes: list[SupplierQuoteIn] = []
    for readable, result in zip(readables, await extractor.read_all(readables), strict=True):
        if isinstance(result, ExtractionError):
            failures.append(f"{readable.file_name}: {result}")
            continue
        quotes.append(
            SupplierQuoteIn(
                supplier_name=(result.supplier_name or readable.file_name)[:200],
                quote_number=_txt(result.quote_number, 100),
                quote_date=_txt(result.quote_date, 40),
                currency=(told or result.currency or request.currency or "AED").upper()[:3],
                validity=_txt(result.validity),
                delivery_time=_txt(result.delivery_time),
                payment_terms=_txt(result.payment_terms),
                warranty=_txt(result.warranty),
                incoterms=_txt(result.incoterms, 60),
                contact=_txt(result.contact, 200),
                discount=_dec(result.discount),
                freight=_dec(result.freight),
                tax=_dec(result.tax),
                quoted_total=_dec(result.quoted_total),
                source=QuoteSource.UPLOAD,
                file_name=readable.file_name,
                extraction_note=_txt(result.note),
                items=[
                    SupplierItemIn(
                        description=item.description,
                        part_number=_txt(item.part_number, 120),
                        brand=_txt(item.brand, 120),
                        unit=_txt(item.unit, 40),
                        quantity=_dec(item.quantity) or 0,
                        unit_price=_dec(item.unit_price) or 0,
                        line_total=_dec(item.line_total),
                        lead_time=_txt(item.lead_time),
                    )
                    for item in result.items
                ],
            )
        )

    if not quotes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No supplier quote could be read. " + ("; ".join(failures) or ""),
        )

    return await _attach(
        session, request, quotes, originals=originals, failures=failures,
        extractor=extractor, drive=drive, zoho=zoho, user=user, roles=roles,
    )


@router.post(
    "/{request_id}/supplier-quotes/typed",
    response_model=QuoteRequestOut,
    summary="Attach supplier quotes typed in by hand and compare them",
)
async def attach_typed_suppliers(
    request_id: uuid.UUID,
    payload: TypedSupplierQuotesIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    extractor: Extractor,
    drive: Drive,
    zoho: Zoho,
) -> QuoteRequestOut:
    """The offer that came as a photograph, a screenshot or a phone call.

    Typed on the screen into the same shape an uploaded document is read into,
    and compared and attached by the same code — so the quote does not know or
    care which way its supplier prices arrived.
    """
    request = await _load(session, request_id)
    try:
        service.require_editable(request, user=user)
    except QuoteError as exc:
        raise _translate(exc) from exc
    quotes = [q.model_copy(update={"source": QuoteSource.MANUAL}) for q in payload.quotes]
    for quote in quotes:
        if not quote.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{quote.supplier_name} has no priced lines.",
            )
    return await _attach(
        session, request, quotes, originals={}, failures=[],
        extractor=extractor, drive=drive, zoho=zoho, user=user, roles=roles,
    )


async def _follow_offer_currency(
    session: AsyncSession,
    request: QuoteRequest,
    quotes: list[SupplierQuoteIn],
    zoho: ZohoBooks,
    failures: list[str],
) -> None:
    """The quote takes the supplier's currency, rather than converting theirs.

    A supplier who quotes in dollars is bought in dollars, and the quote that
    follows is priced in dollars — the reference report was, and the user
    asked for it outright. So when every offer attached shares one currency
    and it is not the quote's, the quote switches to it. A quote with nothing
    priced on it yet is simply relabelled; one that already carries figures
    is converted, every one of them, at Zoho's rate, the way the currency
    cell on the summary sheet does it. Offers in mixed currencies leave the
    quote as it is and are converted for the comparison instead.
    """
    ours = (request.currency or "AED").upper()
    currencies = {(q.currency or ours).upper() for q in quotes}
    if len(currencies) != 1:
        return
    theirs = currencies.pop()
    if theirs == ours:
        return
    nothing_priced = (
        not request.items
        and not request.cost_lines
        and not (request.discount or 0)
        and not (request.shipping_charge or 0)
        and not (request.adjustment or 0)
    )
    if nothing_priced:
        request.currency = theirs
        request.fx_rate = None
        request.supplier_currency = None
        logger.info("quote %s: now in %s, as the supplier quotes", request.id, theirs)
        return
    try:
        await service.convert_currency(
            session,
            request,
            to_currency=theirs,
            rates=lambda a, b: zoho_rate(zoho, quote_currency=a, supplier_currency=b),
        )
    except (QuoteError, ZohoError) as exc:
        failures.append(
            f"The quote stays in {ours}: it could not be switched to {theirs} — {exc}"
        )
    except Exception as exc:  # noqa: BLE001 - the comparison still happens, converted
        logger.warning("quote %s: switch to %s failed: %s", request.id, theirs, exc)
        failures.append(f"The quote stays in {ours}: it could not be switched to {theirs}.")


async def _convert_offers(
    request: QuoteRequest, quotes: list[SupplierQuoteIn], zoho: ZohoBooks, failures: list[str]
) -> None:
    """An offer in another currency is compared at Zoho's rate, not at 1.

    The comparison module leaves a foreign quote at a rate of 1 until a
    person sets one, which on its own screen is a prompt. On a quote request
    it was a dollar figure wearing a dirham sign, and the cheapest column.
    So the rate is read here, once per currency, as "units of the quote's
    currency per one of theirs" — the shape the comparison wants. A rate
    Zoho cannot give is said in the failures rather than guessed.
    """
    ours = (request.currency or "AED").upper()
    rates: dict[str, Any] = {}
    for quote in quotes:
        theirs = (quote.currency or ours).upper()
        if theirs == ours or (quote.fx_rate and quote.fx_rate != 1):
            continue
        if theirs not in rates:
            try:
                # quote_currency=theirs gives "1 THEIRS = x OURS": x is what
                # the comparison multiplies their prices by.
                rates[theirs] = (
                    await zoho_rate(zoho, quote_currency=theirs, supplier_currency=ours)
                ).rate
            except (FxUnavailableError, ZohoError) as exc:
                rates[theirs] = None
                failures.append(f"{quote.supplier_name}: quoted in {theirs}, not converted — {exc}")
            except Exception as exc:  # noqa: BLE001 - a comparison at 1 beats none
                rates[theirs] = None
                logger.warning("rate %s->%s for the comparison failed: %s", theirs, ours, exc)
                failures.append(f"{quote.supplier_name}: quoted in {theirs}, not converted.")
        if rates[theirs] is not None:
            quote.fx_rate = rates[theirs]


async def _attach(
    session: AsyncSession,
    request: QuoteRequest,
    quotes: list[SupplierQuoteIn],
    *,
    originals: dict[str, tuple[bytes, str | None]],
    failures: list[str],
    extractor: QuoteExtractor,
    drive: QuoteDrive,
    zoho: ZohoBooks,
    user,
    roles: set[str],
) -> QuoteRequestOut:
    """Compare the supplier quotes, attach the comparison, file the originals."""
    await _follow_offer_currency(session, request, quotes, zoho, failures)
    await _convert_offers(request, quotes, zoho, failures)
    try:
        comparison = await comparison_service.save(
            session,
            extractor,
            ComparisonIn(
                title=f"Supplier quotes for {request.title}",
                reference=request.reference_number,
                currency=request.currency,
                quotes=quotes,
            ),
            author=user,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Could not compare: {exc}"
        ) from exc

    # The object, not the id: assigning it leaves the relationship loaded, so
    # the response can be built without going back to the database for it.
    request.comparison = comparison
    request.multiple_supplier_quotes = True

    # File the originals into the task's folder. The library is the store, so
    # a document that cannot be filed is not attached: the whole upload is
    # refused with the reason, and nothing half-done is left behind.
    for saved in comparison.quotes:
        original = originals.get(saved.file_name or "")
        if original is None:
            continue
        try:
            document = await filing.file_upload(
                session,
                drive,
                request,
                kind=DocumentKind.SUPPLIER_QUOTE,
                file_name=saved.file_name or "supplier-quote",
                content=original[0],
                content_type=original[1],
                user=user,
                supplier_quote_id=saved.id,
            )
        except DriveError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"{saved.file_name}: could not be filed in the shared library. {exc}",
            ) from exc
        saved.drive_item_id = document.drive_item_id
        saved.drive_url = document.drive_url

    if failures:
        note = "Could not read: " + "; ".join(failures)
        request.notes = f"{request.notes}\n{note}" if request.notes else note
    await session.flush()
    return await _out(session, request, user=user, roles=roles)


@router.post(
    "/{request_id}/select-supplier",
    response_model=QuoteRequestOut,
    summary="Choose the supplier and price the quote from their lines",
)
async def select_supplier(
    request_id: uuid.UUID,
    payload: SupplierChoiceIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    zoho: Zoho,
) -> QuoteRequestOut:
    """Take one supplier's offer as this quote's own lines.

    Their unit price becomes each line's cost and the selling rate is that
    divided by one less ``markup_percent`` — the margin, as a share of the
    selling price — so the margin is visible on every line from the start.
    Nothing here is final: they are ordinary lines afterwards — edit them, add
    to them, delete them, reprice them, through ``PATCH``.

    Choosing again replaces the lines with the other supplier's, which is what
    choosing means. An approver can overrule the choice when they decide, and
    the lines follow them too.
    """
    request = await _load(session, request_id)
    try:
        await service.select_supplier(
            session,
            request,
            user=user,
            supplier_quote_id=payload.supplier_quote_id,
            margin_percent=payload.markup_percent,
            # A supplier in another currency puts the quote in it; what was
            # already on the quote is restated at Zoho's rate, the one the
            # estimate will be converted at.
            rates=lambda ours, theirs: zoho_rate(
                zoho, quote_currency=ours, supplier_currency=theirs
            ),
            roles=roles,
        )
    except QuoteError as exc:
        raise _translate(exc) from exc
    except ZohoError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read Zoho Books' currency table to convert the offer.",
        ) from exc
    return await _out(session, request, user=user, roles=roles)


# ── 2b. every other document ───────────────────────────────────────────


@router.post(
    "/{request_id}/documents",
    response_model=QuoteRequestOut,
    summary="Upload documents against the quote and file them with the task",
)
async def upload_documents(
    request_id: uuid.UUID,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    extractor: Extractor,
    drive: Drive,
    zoho: Zoho,
    text_model: Model,
    files: Annotated[list[UploadFile], File(description="Any document about this quote")],
    kind: Annotated[
        str, Form(description="What they are: customer_rfq, end_user_po, freight_quote…")
    ] = "other",
    notes: Annotated[str | None, Form()] = None,
) -> QuoteRequestOut:
    """The customer's RFQ, the end user's PO, a courier quote, a datasheet —
    anything that belongs with the quote.

    Each file is put in the task's folder in the shared library and recorded
    on the quote, then read for whatever its kind can give: an RFQ's reference
    and closing date, a courier quote's freight figure. What is read arrives
    as suggestions, never written onto the quote by itself.

    Supplier quotations are the one kind with more to do — they are read into
    prices and compared — so that kind is handed to the supplier-quote route.
    """
    request = await _load(session, request_id)
    try:
        service.require_editable(request, user=user)
    except QuoteError as exc:
        raise _translate(exc) from exc
    try:
        which = DocumentKind(kind.strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{kind!r} is not a document kind. One of: "
            + ", ".join(k.value for k in DocumentKind if k is not DocumentKind.COSTING_REPORT),
        ) from exc
    if which is DocumentKind.COSTING_REPORT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The costing report is filed by the system when the quote is sent.",
        )
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Attach a file.")
    if len(files) > MAX_UPLOADS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_UPLOADS} documents at a time",
        )
    if which is DocumentKind.SUPPLIER_QUOTE:
        return await attach_suppliers(
            request_id, user, roles, session, extractor, drive, zoho, files
        )

    from app.quoting import reading

    for upload in files:
        name = upload.filename or "unnamed"
        content = await upload.read()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"{name} is empty."
            )
        try:
            document = await filing.file_upload(
                session,
                drive,
                request,
                kind=which,
                file_name=name,
                content=content,
                content_type=upload.content_type,
                user=user,
                notes=notes,
            )
        except DriveError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"{name}: could not be filed in the shared library. {exc}",
            ) from exc
        # What the document says, offered rather than applied.
        await reading.read_into(
            document, request, name, content, upload.content_type, model=text_model
        )
    await session.flush()
    return await _out(session, request, user=user, roles=roles)


@router.post(
    "/{request_id}/documents/{document_id}/apply",
    response_model=QuoteRequestOut,
    summary="Write a document's suggested values onto the quote",
)
async def apply_suggestions(
    request_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: ApplySuggestionsIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
) -> QuoteRequestOut:
    """The person accepts what was read. Blank fields are filled; a field
    somebody typed is left alone unless ``overwrite`` says otherwise."""
    from app.quoting import reading

    request = await _load(session, request_id)
    try:
        service.require_editable(request, user=user)
    except QuoteError as exc:
        raise _translate(exc) from exc
    document = next((d for d in request.documents if d.id == document_id), None)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such document")
    try:
        applied = reading.apply(request, document, payload.fields, overwrite=payload.overwrite)
    except QuoteError as exc:
        raise _translate(exc) from exc
    await session.flush()
    logger.info("quote %s: applied %s from %s", request.id, applied, document.file_name)
    return await _out(session, request, user=user, roles=roles)


@router.delete(
    "/{request_id}/documents/{document_id}",
    response_model=QuoteRequestOut,
    summary="Take a document off the quote and out of the folder",
)
async def delete_document(
    request_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    drive: Drive,
    extractor: Extractor,
) -> QuoteRequestOut:
    """Only while the quote is editable, and only for documents a person
    uploaded — the costing report is the system's and is replaced on the
    next send.

    A supplier quotation goes with its prices: the row it was read into
    leaves the comparison, which is worked out again over what is left, and
    if it was the offer this quote was priced from the choice is cleared —
    the lines stay, as ordinary lines, and a supplier has to be chosen again
    before the quote can be sent. With no supplier quotes left the
    comparison itself goes.
    """
    request = await _load(session, request_id)
    try:
        service.require_editable(request, user=user)
    except QuoteError as exc:
        raise _translate(exc) from exc
    document = next((d for d in request.documents if d.id == document_id), None)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such document")
    if document.kind == DocumentKind.COSTING_REPORT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The costing report is filed by the system and replaced on the next send.",
        )
    if document.kind == DocumentKind.SUPPLIER_QUOTE and document.supplier_quote_id is not None:
        await _detach_supplier_quote(session, request, extractor, document.supplier_quote_id)
    await filing.remove_document(drive, request, document)
    await session.flush()
    return await _out(session, request, user=user, roles=roles)


@router.delete(
    "/{request_id}/supplier-quotes/{supplier_quote_id}",
    response_model=QuoteRequestOut,
    summary="Take one supplier's offer off the comparison",
)
async def remove_supplier_quote(
    request_id: uuid.UUID,
    supplier_quote_id: uuid.UUID,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    drive: Drive,
    extractor: Extractor,
) -> QuoteRequestOut:
    """The offer that was read wrong, typed wrong, or is simply not wanted.

    Its prices leave the comparison, which is worked out again over what is
    left; its document, if it had one, leaves the quote and the folder. If it
    was the offer the quote was priced from, the choice is cleared and the
    lines stay as ordinary lines. Only while the quote is still editable.
    """
    request = await _load(session, request_id)
    try:
        service.require_editable(request, user=user)
    except QuoteError as exc:
        raise _translate(exc) from exc
    if request.comparison is None or not any(
        q.id == supplier_quote_id for q in request.comparison.quotes
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That supplier quote is not on this request's comparison.",
        )
    document = next(
        (d for d in request.documents if d.supplier_quote_id == supplier_quote_id), None
    )
    await _detach_supplier_quote(session, request, extractor, supplier_quote_id)
    if document is not None:
        await filing.remove_document(drive, request, document)
    await session.flush()
    return await _out(session, request, user=user, roles=roles)


def _quote_in_of(row) -> SupplierQuoteIn:
    """A stored supplier quote as the comparison takes it, so the analysis can
    be worked out again over the rows that remain."""
    return SupplierQuoteIn(
        supplier_name=row.supplier_name,
        quote_number=row.quote_number,
        quote_date=row.quote_date,
        currency=row.currency,
        fx_rate=row.fx_rate,
        validity=row.validity,
        delivery_time=row.delivery_time,
        payment_terms=row.payment_terms,
        warranty=row.warranty,
        incoterms=row.incoterms,
        contact=row.contact,
        notes=row.notes,
        discount=row.discount,
        freight=row.freight,
        tax=row.tax,
        quoted_total=row.quoted_total,
        source=row.source,
        file_name=row.file_name,
        extraction_note=row.extraction_note,
        items=[
            SupplierItemIn(
                description=item.description,
                part_number=item.part_number,
                brand=item.brand,
                unit=item.unit,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
                lead_time=item.lead_time,
            )
            for item in row.items
        ],
    )


async def _detach_supplier_quote(
    session: AsyncSession, request: QuoteRequest, extractor: QuoteExtractor, quote_id: uuid.UUID
) -> None:
    """Take one supplier's offer out of the quote's comparison."""
    comparison = request.comparison
    if comparison is None:
        return
    row = next((q for q in comparison.quotes if q.id == quote_id), None)
    if row is None:
        return
    comparison.quotes.remove(row)
    if request.selected_supplier_quote_id == row.id:
        # The lines it priced stay — they are the quote's own now — but the
        # choice is gone, and ``why_not_submit`` will ask for one again.
        request.selected_supplier_quote_id = None
    for item in request.items:
        if item.source_supplier_quote_id == row.id:
            item.source_supplier_quote_id = None
    if comparison.quotes:
        comparison.analysis = await comparison_service.compare(
            extractor,
            [_quote_in_of(q) for q in comparison.quotes],
            currency=comparison.currency,
            ids=[str(q.id) for q in comparison.quotes],
        )
        comparison.analysed_at = datetime.now(UTC)
    else:
        request.comparison = None
        request.comparison_id = None
        request.multiple_supplier_quotes = False
        await session.delete(comparison)
    logger.info("quote %s: supplier quote %s removed", request.id, row.supplier_name)


# ── 3. approval ────────────────────────────────────────────────────────


def _quote_link(request: QuoteRequest) -> str:
    """Where to read this quote. Follows the deployment, not a second setting."""
    return f"{get_settings().frontend_url.rstrip('/')}/quote-requests/{request.id}"


async def _notify(
    session: AsyncSession,
    request: QuoteRequest,
    send: Callable[[], Awaitable[Any]],
    *,
    stamp: bool = False,
) -> None:
    """Send a notification. **Never fails the thing it is about.**

    A quote that is approved is approved whether or not the mail went. But a
    silent failure hides the one fact that matters — that nobody was told — so
    what went wrong is kept on the row, where somebody can find it while asking
    about that particular quote.
    """
    if not get_settings().notify_by_email:
        return
    try:
        await send()
        if stamp:
            request.approvers_notified_at = datetime.now(UTC)
        request.notify_error = None
    except Exception as exc:  # noqa: BLE001 - the quote still stands
        logger.warning("notification failed for quote %s: %s", request.id, exc)
        request.notify_error = f"{type(exc).__name__}: {exc}"[:500]
    await session.flush()


def _addresses(*people, without: uuid.UUID | None = None) -> list[str]:
    """Their email addresses, deduplicated, minus whoever caused the event.

    Nobody needs mail about the thing they just did, and a person who is both
    the requester and an approver would otherwise get two copies of it.
    """
    seen: dict[uuid.UUID, str] = {}
    for person in people:
        if person is not None and person.email and person.id != without:
            seen.setdefault(person.id, person.email)
    return list(seen.values())


async def _notify_approvers(
    session: AsyncSession,
    mailer: QuoteMailer,
    request: QuoteRequest,
    *,
    without: uuid.UUID | None = None,
    report=None,
) -> None:
    """Tell the people who can decide it that it is waiting, with the selling
    & costing report attached so the case is in their hands with the ask."""
    people = await service.approvers_for(session, request.team_id)
    await _notify(
        session,
        request,
        lambda: mailer.send_for_approval(
            request,
            _addresses(*people, without=without),
            link=_quote_link(request),
            report=report,
        ),
        stamp=True,
    )


@router.post(
    "/{request_id}/submit", response_model=QuoteRequestOut, summary="Send for approval"
)
async def submit(
    request_id: uuid.UUID,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    mailer: Mailer,
    zoho: Zoho,
    drive: Drive,
) -> QuoteRequestOut:
    """Hand it to the approvers, and tell them so.

    The people who can decide it are emailed a link straight to the quote and
    the selling & costing report as a PDF. Not the requester, even when they
    are also an approver — nobody needs mail about the thing they just did.
    """
    request = await _load(session, request_id)
    try:
        await service.submit(session, request, user=user)
    except QuoteError as exc:
        raise _translate(exc) from exc

    # The report for this pass: rendered once, filed with the task's other
    # paperwork, and mailed. Neither the filing nor the mail can stop the
    # submission — see ``filing.file_report`` and ``_notify``.
    report = None
    try:
        report = await _report(request, zoho)
        await filing.file_report(
            session,
            drive,
            request,
            content=report_pdf.render(report),
            reference=report.reference,
            user=user,
        )
    except Exception:  # noqa: BLE001 - the approvers are still told
        logger.exception("costing report for quote %s could not be built", request.id)
    await _notify_approvers(session, mailer, request, without=user.id, report=report)
    return await _out(session, request, user=user, roles=roles)


@router.post(
    "/{request_id}/reviews",
    response_model=QuoteRequestOut,
    summary="Approve, reject, send back, or comment",
)
async def review(
    request_id: uuid.UUID,
    payload: ReviewIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    mailer: Mailer,
) -> QuoteRequestOut:
    """An approver's decision, and the requester hears about it.

    ``rework`` sends it back to the requester and the loop goes round again;
    ``comment`` decides nothing and leaves it where it is. Approving a quote with
    several supplier offers means naming the one that won.

    The decision is mailed to whoever raised it and whoever holds it now, from
    the approver's own mailbox — a rejection from a system address is an
    argument nobody can have.
    """
    request = await _load(session, request_id)
    try:
        record = await service.review(
            session,
            request,
            reviewer=user,
            roles=roles,
            action=payload.action,
            note=payload.note,
            selected_supplier_quote_id=payload.selected_supplier_quote_id,
        )
    except QuoteError as exc:
        raise _translate(exc) from exc

    await _notify(
        session,
        request,
        lambda: mailer.send_decision(
            request,
            _addresses(request.created_by, request.assigned_to, without=user.id),
            link=_quote_link(request),
            review=record,
        ),
    )
    return await _out(session, request, user=user, roles=roles)


@router.post(
    "/{request_id}/negotiate",
    response_model=QuoteRequestOut,
    summary="Reopen an approved quote because the customer came back",
)
async def negotiate(
    request_id: uuid.UUID,
    payload: NegotiationIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    zoho: Zoho,
    rates: Rates,
    mailer: Mailer,
) -> QuoteRequestOut:
    """Another round on a quote that was already approved.

    The round that was approved is kept whole in ``revisions`` — its lines, its
    totals and the win probability it carried — so the next reviewer is arguing
    about numbers they can still see rather than ones somebody remembers. What
    the customer is asking for is recorded in the review history beside them.

    The probability is read again for the new round, because the estimate
    history has moved on since the quote was raised. The old one stays with the
    round it belonged to.
    """
    request = await _load(session, request_id)
    try:
        await service.open_negotiation(
            session, request, user=user, roles=roles, note=payload.note
        )
    except QuoteError as exc:
        raise _translate(exc) from exc

    estimate = await rates.estimate(zoho, customer_name=request.customer_name)
    request.win_probability = estimate.probability
    request.win_basis = estimate.basis
    await session.flush()

    # The approvers are told, because what they approved is about to change and
    # their approval was of the old numbers.
    people = await service.approvers_for(session, request.team_id)
    await _notify(
        session,
        request,
        lambda: mailer.send_decision(
            request,
            _addresses(*people, without=user.id),
            link=_quote_link(request),
            review=request.reviews[-1],
        ),
    )
    return await _out(session, request, user=user, roles=roles)


@router.get(
    "/{request_id}/approvers",
    response_model=list[str],
    summary="Who can decide this quote",
)
async def approvers(
    request_id: uuid.UUID, _: CurrentUser, session: Session
) -> list[str]:
    request = await _load(session, request_id)
    return sorted(u.display_name for u in await service.approvers_for(session, request.team_id))


# ── comments on anything ───────────────────────────────────────────────


@router.post(
    "/{request_id}/comments",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Comment on a field, a line, a supplier quote, or the whole quote",
)
async def add_comment(
    request_id: uuid.UUID,
    payload: CommentIn,
    user: CurrentUser,
    session: Session,
    mailer: Mailer,
) -> CommentOut:
    """Anchored, not floating.

    "This rate looks wrong" is useful on the rate and nearly useless in a list at
    the bottom of the page.

    It is also mailed to the other side of the quote — an approver's question
    goes to the people who own it, and their answer goes back to the approvers.
    A remark nobody is told about is a remark waiting for somebody to happen to
    scroll past it.
    """
    request = await _load(session, request_id)
    try:
        row = await service.comment(
            session,
            request,
            author=user,
            body=payload.body,
            target_type=payload.target_type,
            target_ref=payload.target_ref,
        )
    except QuoteError as exc:
        raise _translate(exc) from exc

    owners = (request.created_by_id, request.assigned_to_id)
    if user.id in owners:
        people = await service.approvers_for(session, request.team_id)
    else:
        people = [request.created_by, request.assigned_to]
    await _notify(
        session,
        request,
        lambda: mailer.send_comment(
            request,
            _addresses(*people, without=user.id),
            link=_quote_link(request),
            comment=row,
        ),
    )

    out = CommentOut.model_validate(row)
    out.author_name = user.display_name
    return out


@router.post(
    "/{request_id}/comments/{comment_id}/resolve",
    response_model=CommentOut,
    summary="Mark a comment dealt with",
)
async def resolve(
    request_id: uuid.UUID, comment_id: uuid.UUID, user: CurrentUser, session: Session
) -> CommentOut:
    request = await _load(session, request_id)
    row = next((c for c in request.comments if c.id == comment_id), None)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such comment on this quote"
        )
    await service.resolve_comment(session, row, user=user)
    out = CommentOut.model_validate(row)
    out.author_name = row.author.display_name if row.author else None
    return out


# ── reading ────────────────────────────────────────────────────────────


@router.get("", response_model=list[QuoteSummaryOut], summary="Quote requests")
async def index(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    team: Annotated[str | None, Query(description="Team handle or id")] = None,
    quote_status: Annotated[QuoteStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[QuoteSummaryOut]:
    """The quotes this person may see: their own, and the ones they decide.

    Not everyone's. Approvers, team leads and managers of a team see that
    team's; a super admin, the CEO or a manager sees all. The rule is the one
    ``may_approve`` uses, so a quote never waits on somebody who cannot see it.
    """
    team_id = None
    if team:
        try:
            team_id = (await teams_service.get_team(session, team)).id
        except TeamError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    rows = await service.listing(
        session,
        team_id=team_id,
        status=quote_status,
        viewer=user,
        viewer_roles=roles,
        limit=limit,
    )
    return [_summary(r, roles) for r in rows]


@router.get("/mine", response_model=list[QuoteSummaryOut], summary="Quotes I raised or owe work on")
async def mine(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[QuoteSummaryOut]:
    return [
        _summary(r, roles)
        for r in await service.listing(session, mine_for=user.id, limit=limit)
    ]


@router.get(
    "/queue",
    response_model=list[QuoteSummaryOut],
    summary="Approved and waiting to be created in Zoho",
)
async def queue(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    team: Annotated[str | None, Query(description="Team handle or id")] = None,
    mine_only: Annotated[bool, Query(description="Only the ones assigned to me")] = False,
) -> list[QuoteSummaryOut]:
    """The queue, and where this module stops.

    Nothing pushes to Zoho. The list is live and that step is deliberately a
    later piece of work — these are the quotes that are ready for it.
    """
    team_id = None
    if team:
        try:
            team_id = (await teams_service.get_team(session, team)).id
        except TeamError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    rows = await service.queue(
        session, team_id=team_id, assignee=user.id if mine_only else None
    )
    return [_summary(r, roles) for r in rows]


@router.get("/{request_id}", response_model=QuoteRequestOut, summary="One quote in full")
async def detail(
    request_id: uuid.UUID, user: CurrentUser, roles: CurrentRoles, session: Session
) -> QuoteRequestOut:
    """Everything an approver needs: the quote, its lines and margins, the
    supplier comparison, every comment and the whole review history."""
    return await _out(session, await _load(session, request_id), user=user, roles=roles)


@router.get(
    "/{request_id}/items", response_model=list[ItemOut], summary="Just the lines"
)
async def items(request_id: uuid.UUID, _: CurrentUser, session: Session) -> list[ItemOut]:
    return [ItemOut.model_validate(i) for i in (await _load(session, request_id)).items]


@router.get(
    "/{request_id}/reviews", response_model=list[ReviewOut], summary="The review history"
)
async def reviews(request_id: uuid.UUID, _: CurrentUser, session: Session) -> list[ReviewOut]:
    request = await _load(session, request_id)
    out = []
    for row in request.reviews:
        item = ReviewOut.model_validate(row)
        item.reviewer_name = row.reviewer.display_name if row.reviewer else None
        out.append(item)
    return out
