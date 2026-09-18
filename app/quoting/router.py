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

**Nothing here touches Zoho.** The list is live; the push is a later piece of
work and the queue is deliberately where this stops. The only Zoho traffic is a
read of past estimates to work out a win probability.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
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
from app.models.quoting import QuoteRequest, QuoteStatus
from app.proposals import mirror
from app.proposals.router import get_sharepoint
from app.proposals.sharepoint import SharePointError, SharePointProposals
from app.quoting import bidpack, service, storage
from app.quoting import calculation as calc
from app.quoting.fx import FxUnavailableError, zoho_rate
from app.quoting import workbook as workbook_mod
from app.quoting.mailer import QuoteMailer
from app.quoting.storage import QuoteDrive
from app.quoting.probability import WinRates
from app.quoting.schemas import (
    BidPackOut,
    CalcStepOut,
    CommentIn,
    CommentOut,
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
    RepriceIn,
    ReviewIn,
    ReviewOut,
    SupplierChoiceIn,
    TaskQuoteIn,
)
from app.quoting.service import QuoteError, QuoteNotFoundError, QuotePermissionError
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


def get_drive(request: Request) -> QuoteDrive:
    return request.app.state.quote_drive


Extractor = Annotated[QuoteExtractor, Depends(get_extractor)]
Zoho = Annotated[ZohoBooks, Depends(get_zoho)]
Rates = Annotated[WinRates, Depends(get_win_rates)]
SharePoint = Annotated[SharePointProposals, Depends(get_sharepoint)]
Mailer = Annotated[QuoteMailer, Depends(get_mailer)]
Drive = Annotated[QuoteDrive, Depends(get_drive)]


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
    pack = bidpack.build(request)
    body.bid = BidPackOut.model_validate(pack, from_attributes=True)
    # And the working behind every figure, from the same pass.
    body.calculation = [
        CalcStepOut.model_validate(step, from_attributes=True)
        for step in calc.steps(request, pack)
    ]

    # The uploaded documents, from the supplier quote rows themselves. Both
    # relationships are selectin-loaded, so this costs no extra query and — more
    # to the point — no lazy load from inside async code.
    if request.comparison is not None:
        body.documents = [
            QuoteDocumentOut(
                supplier_quote_id=quote.id,
                supplier_name=quote.supplier_name,
                file_name=quote.file_name,
                file_type=quote.file_type,
                drive_url=quote.drive_url,
                is_selected=quote.id == request.selected_supplier_quote_id,
            )
            for quote in request.comparison.quotes
        ]

    body.may_edit = request.is_editable and request.created_by_id == user.id
    body.submit_reason = service.why_not_submit(request)
    body.may_submit = body.may_edit and body.submit_reason is None
    allowed, reason = await service.may_approve(session, request, user=user, roles=roles)
    body.may_approve = allowed
    body.approve_reason = None if allowed else reason
    # Its own rule: whose quote it is, plus a super admin, and no status in
    # it at all. See ``service.may_set_currency``.
    body.may_set_currency = service.may_set_currency(request, user=user, roles=roles)
    body.may_reprice = service.may_reprice(request, user=user, roles=roles)
    # Asked here rather than re-derived on the screen, like every other
    # permission on this body. A frontend that works out for itself who may
    # delete something is a frontend that will one day disagree with the server.
    body.may_delete = SUPER_ADMIN in set(roles)
    return body


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
    from_currency: Annotated[str, Query(alias="from", min_length=3, max_length=3)],
    to_currency: Annotated[str, Query(alias="to", min_length=3, max_length=3)],
) -> FxQuoteOut:
    """The rate Zoho will convert the estimate at, so the bid is costed at the
    same one. Shown with its working — both sides against Zoho's base — so a
    person can see it is the ratio of two figures somebody set in Zoho, not a
    number this app made up. See ``app.quoting.fx``."""
    try:
        found = await zoho_rate(zoho, from_currency=from_currency, to_currency=to_currency)
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
        service.require_editable(request, user=user)
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
) -> QuoteRequestOut:
    """The one field that still moves on a quote that has gone up.

    Everything else freezes the moment a quote is submitted, and for a good
    reason: an approver has to decide on the document they were sent, not on one
    that changed while they read it.

    The currency is deliberately outside that rule. Nothing in this module
    converts between currencies — the code is a *label* on figures that are
    already what they are — so changing it restates no number and invalidates no
    approval. The alternative was worse: a quote discovered to be in the wrong
    currency could only be corrected by pulling it back out of approval, which
    means re-notifying every approver to fix a three-letter code.

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
    request.currency = payload.currency
    await session.flush()
    return await _out(session, request, user=user, roles=roles)


@router.post(
    "/{request_id}/reprice",
    response_model=QuoteRequestOut,
    summary="Re-price from the chosen supplier at Zoho's rate, in any state",
)
async def reprice(
    request_id: uuid.UUID,
    payload: RepriceIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    zoho: Zoho,
) -> QuoteRequestOut:
    """Put the figures at the rate the estimate will be converted at.

    For a quote priced before the rate came from Zoho — by hand, at whatever
    rate somebody remembered — and for one whose rate in Zoho has since moved.
    The supplier, the lines and the markup stay what they were; only the
    conversion changes, and Zoho's rate replaces the bid's. Allowed in every
    state, drafts included, by whoever raised or holds the quote and by a super
    admin, because it corrects the figures without changing a decision.
    """
    request = await _load(session, request_id)
    if not service.may_reprice(request, user=user, roles=roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This quote is not yours to re-price."
                if request.selected_supplier_quote_id is not None
                else "No supplier has been chosen for this quote, so there is nothing "
                "to re-price from."
            ),
        )
    try:
        await service.reprice_at_rate(
            session,
            request,
            rates=lambda src, dst: zoho_rate(zoho, from_currency=src, to_currency=dst),
            markup_percent=payload.markup_percent,
        )
    except QuoteError as exc:
        raise _translate(exc) from exc
    except ZohoError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read Zoho Books' currency table to re-price.",
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
    files: Annotated[
        list[UploadFile] | None,
        File(description="Supplier quotes: PDF, image, XLSX, CSV or DOCX"),
    ] = None,
) -> QuoteRequestOut:
    """Upload what the suppliers sent, or post them typed in.

    Each document is read locally first and only goes to a model if that fails,
    exactly as the comparison module does — this is that module, not a second
    copy of it. The comparison is computed and attached; which supplier wins is
    an approver's decision, not this endpoint's.
    """
    request = await _load(session, request_id)
    try:
        service.require_editable(request, user=user)
    except QuoteError as exc:
        raise _translate(exc) from exc

    uploads = files or []
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
            readables.append(prepare(name, raw, upload.content_type))
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
                currency=(result.currency or request.currency or "AED").upper()[:3],
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

    # File the originals into the drive, if one is configured. Deliberately
    # after the comparison is saved and deliberately unable to fail it: the
    # person gets their comparison whether or not a drive was reachable, and
    # the database copy is the system of record either way.
    if drive.enabled:
        folder = storage.folder_for(request.reference, request.title, request.id)
        for saved in comparison.quotes:
            original = originals.get(saved.file_name or "")
            if original is None:
                continue
            filed = await drive.try_file(
                folder=folder,
                filename=saved.file_name or "supplier-quote",
                content=original[0],
                content_type=original[1],
            )
            if filed is not None:
                saved.drive_item_id = filed.item_id
                saved.drive_url = filed.web_url

    # The object, not the id: assigning it leaves the relationship loaded, so
    # the response can be built without going back to the database for it.
    request.comparison = comparison
    request.multiple_supplier_quotes = True
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

    Their unit price becomes each line's cost and the selling rate is that plus
    ``markup_percent``, so the margin is visible on every line from the start.
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
            markup_percent=payload.markup_percent,
            # An offer in another currency is converted at Zoho's rate — the
            # one the estimate will be converted at — unless the bid has one.
            rates=lambda src, dst: zoho_rate(zoho, from_currency=src, to_currency=dst),
        )
    except QuoteError as exc:
        raise _translate(exc) from exc
    except ZohoError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read Zoho Books' currency table to convert the offer.",
        ) from exc
    return await _out(session, request, user=user, roles=roles)


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
) -> None:
    """Tell the people who can decide it that it is waiting."""
    people = await service.approvers_for(session, request.team_id)
    await _notify(
        session,
        request,
        lambda: mailer.send_for_approval(
            request, _addresses(*people, without=without), link=_quote_link(request)
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
) -> QuoteRequestOut:
    """Hand it to the approvers, and tell them so.

    The people who can decide it are emailed a link straight to the quote. Not
    the requester, even when they are also an approver — nobody needs mail about
    the thing they just did.
    """
    request = await _load(session, request_id)
    try:
        await service.submit(session, request, user=user)
    except QuoteError as exc:
        raise _translate(exc) from exc
    await _notify_approvers(session, mailer, request, without=user.id)
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
