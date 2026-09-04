"""Supplier quote comparison, for presales.

The flow this serves, in the order an engineer actually works:

1. ``POST /comparisons/extract`` — upload the quotes that came in. Claude reads
   them and hands back drafts. **Nothing is saved.**
2. Correct whatever was misread. Or skip 1 and 2 entirely and type the numbers
   in: the same shapes go to the next step either way.
3. ``POST /comparisons/analyse`` — see the comparison. Still nothing saved.
4. ``POST /comparisons`` — save it, analysis and all.

Extraction and analysis are separate endpoints so that reviewing the extracted
data is a step rather than an afterthought, and so the manual path is a first-
class route rather than a special case. Sending a saved comparison for approval
is the next piece of work and is deliberately absent here.

Access: a signed-in user, gated on the ``quote_comparison`` module so presales
holds it rather than the whole company. The read-only routes also accept an
``X-API-Key`` header for machine callers; anything that writes, or that is
attributed to a person, requires a session.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

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
from app.auth.api_key import has_api_key
from app.auth.deps import CurrentUser
from app.comparison import service
from app.comparison.documents import DocumentError, prepare
from app.comparison.extraction import ExtractionError, QuoteExtractor
from app.comparison.schemas import (
    AnalyseIn,
    AnalysisOut,
    ComparisonIn,
    ComparisonOut,
    ComparisonSummaryOut,
    ExtractionFailure,
    ExtractionOut,
    ItemIn,
    QuoteIn,
)
from app.comparison.service import ComparisonError, ComparisonNotFoundError
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.models.comparison import QuoteSource
from app.roles.deps import CurrentRoles

logger = logging.getLogger("hamdaz.comparison")

router = APIRouter(prefix="/comparisons", tags=["quote comparison"])

Session = Annotated[AsyncSession, Depends(get_session)]

MODULE_KEY = "quote_comparison"

#: More suppliers than this on one requirement is a mistake, and each one is an
#: API call against a real bill.
MAX_UPLOADS = 12


def get_extractor(request: Request) -> QuoteExtractor:
    return request.app.state.quote_extractor


Extractor = Annotated[QuoteExtractor, Depends(get_extractor)]


async def require_module(user: CurrentUser, roles: CurrentRoles, session: Session) -> None:
    """The caller's team must hold the quote comparison module."""
    if not await access_service.can_reach(
        session, user_id=user.id, global_roles=roles, module_key=MODULE_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your team does not have the Quote Comparison module. "
                "A super admin can grant it."
            ),
        )


ModuleAccess = Annotated[None, Depends(require_module)]


async def reader(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Session,
) -> None:
    """A session with the module, or a machine key.

    Read-only routes only. The key is an alternative to proving *who* you are,
    which is fine for reading and not fine for writing.
    """
    if has_api_key(request, settings):
        return
    from app.auth.deps import current_user
    from app.roles.deps import current_roles

    user = await current_user(request, session, settings)
    roles = await current_roles(user, session)
    await require_module(user, roles, session)


ReadAccess = Annotated[None, Depends(reader)]


def _translate(exc: ComparisonError) -> HTTPException:
    if isinstance(exc, ComparisonNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ── 1. reading the documents ───────────────────────────────────────────


@router.post(
    "/extract",
    response_model=ExtractionOut,
    summary="Read uploaded supplier quotes into draft data",
)
async def extract(
    _user: CurrentUser,
    _access: ModuleAccess,
    extractor: Extractor,
    files: Annotated[
        list[UploadFile],
        File(description="Supplier quotes: PDF, image, XLSX, CSV or DOCX"),
    ],
    currency: Annotated[str, Form(description="Comparison currency")] = "AED",
) -> ExtractionOut:
    """Extract, and stop. Nothing is written and nothing is decided.

    The result is a draft for a person to check. Anything the model was unsure
    of arrives in ``extraction_note`` on the quote it belongs to, and a document
    that could not be read appears in ``failed`` while the others still come
    back — one bad scan must not cost three good quotes.
    """
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Attach at least one file"
        )
    if len(files) > MAX_UPLOADS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_UPLOADS} quotes at a time",
        )

    readables, failed = [], []
    for upload in files:
        name = upload.filename or "unnamed"
        try:
            readables.append(prepare(name, await upload.read(), upload.content_type))
        except DocumentError as exc:
            failed.append(ExtractionFailure(file_name=name, error=str(exc)))

    if not readables:
        # Every file was unreadable; that is the whole answer, not a partial one.
        return ExtractionOut(quotes=[], failed=failed, model=None)

    results = await extractor.read_all(readables)

    quotes: list[QuoteIn] = []
    for readable, result in zip(readables, results, strict=True):
        if isinstance(result, ExtractionError):
            failed.append(ExtractionFailure(file_name=readable.file_name, error=str(result)))
            continue
        quotes.append(
            QuoteIn(
                supplier_name=(result.supplier_name or readable.file_name)[:200],
                # blank_to_none: the extraction schema uses "" and 0 to keep
                # its decoding grammar simple, but a stored empty string reads
                # as "the supplier said nothing" rather than "we did not find it".
                quote_number=_txt(result.quote_number, 100),
                quote_date=_txt(result.quote_date, 40),
                # The quote's own currency; fx_rate stays 1 until a person sets
                # it. No rate is fetched — see QuoteIn.fx_rate.
                currency=(result.currency or currency or "AED").upper()[:3],
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
                    ItemIn(
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

    return ExtractionOut(
        quotes=quotes, failed=failed, model=get_settings().extract_model
    )


def _dec(value):
    """A model-supplied amount, with 0 meaning "not on the document"."""
    from app.comparison.extraction import blank_to_none, to_decimal

    return to_decimal(blank_to_none(value))


def _txt(value: str | None, limit: int | None = None) -> str | None:
    """A model-supplied string, with "" meaning "not on the document".

    ``limit`` trims to what the field holds. Only the short ones pass it — a
    part number, an incoterm — where an over-long value means the model wrote a
    sentence where a code belongs. Terms and delivery times are free text and
    are never trimmed: cutting a condition off a quote changes what it says.
    Trimming beats the alternative, which is one stray field failing an upload
    of documents that were read perfectly well.
    """
    from app.comparison.extraction import blank_to_none

    text = blank_to_none((value or "").strip())
    if text is not None and limit is not None and len(text) > limit:
        return text[:limit].rstrip()
    return text


# ── 2. comparing, without saving ───────────────────────────────────────


@router.post("/analyse", response_model=AnalysisOut, summary="Compare quotes without saving")
async def analyse(
    payload: AnalyseIn, _user: CurrentUser, _access: ModuleAccess, extractor: Extractor
) -> AnalysisOut:
    """The comparison itself.

    Takes extracted quotes, hand-typed ones, or a mix — by this point they are
    the same shape. Line items are matched across suppliers by the model; every
    total, spread and saving below it is computed here in exact decimal.
    """
    try:
        result = await service.compare(
            extractor, payload.quotes, currency=payload.currency.upper()
        )
    except ComparisonError as exc:
        raise _translate(exc) from exc
    return AnalysisOut(currency=payload.currency.upper(), analysis=result)


# ── 3. saving ──────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=ComparisonOut,
    status_code=status.HTTP_201_CREATED,
    summary="Save a comparison",
)
async def create(
    payload: ComparisonIn, user: CurrentUser, _access: ModuleAccess, session: Session,
    extractor: Extractor,
) -> ComparisonOut:
    """Store the comparison and re-run the analysis over what was stored.

    The analysis is recomputed rather than accepted from the request, so the
    saved figures always follow from the saved line items.
    """
    try:
        comparison = await service.save(session, extractor, payload, author=user)
    except ComparisonError as exc:
        raise _translate(exc) from exc
    return _out(comparison)


@router.get("", response_model=list[ComparisonSummaryOut], summary="Saved comparisons")
async def index(
    _access: ReadAccess,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ComparisonSummaryOut]:
    """Everyone's, newest first — a comparison is a record colleagues refer to.

    For only your own, use ``/comparisons/mine``, which needs a session because
    it needs to know who you are.
    """
    rows = await service.for_user(session, None, limit=limit)
    return [ComparisonSummaryOut(**service.summarise(c)) for c in rows]


@router.get("/mine", response_model=list[ComparisonSummaryOut], summary="My comparisons")
async def mine(
    user: CurrentUser,
    _access: ModuleAccess,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ComparisonSummaryOut]:
    rows = await service.for_user(session, user.id, limit=limit)
    return [ComparisonSummaryOut(**service.summarise(c)) for c in rows]


@router.get("/{comparison_id}", response_model=ComparisonOut, summary="One comparison")
async def detail(
    comparison_id: uuid.UUID, _access: ReadAccess, session: Session
) -> ComparisonOut:
    try:
        return _out(await service.get(session, comparison_id))
    except ComparisonError as exc:
        raise _translate(exc) from exc


@router.get(
    "/{comparison_id}/quotes/{quote_id}/document",
    summary="The original uploaded quote document",
    response_class=Response,
)
async def document(
    comparison_id: uuid.UUID, quote_id: uuid.UUID, _access: ReadAccess, session: Session
) -> Response:
    """The supplier's own file, as uploaded — what a disputed figure is checked against."""
    try:
        comparison = await service.get(session, comparison_id)
    except ComparisonError as exc:
        raise _translate(exc) from exc

    quote = next((q for q in comparison.quotes if q.id == quote_id), None)
    if quote is None or not quote.file_bytes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No stored document for that quote",
        )

    safe = "".join(
        c for c in (quote.file_name or "quote") if c.isprintable() and c not in {'"', "\\"}
    )
    return Response(
        content=quote.file_bytes,
        media_type=quote.file_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{safe or "quote"}"'},
    )


@router.delete(
    "/{comparison_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a comparison",
)
async def remove(
    comparison_id: uuid.UUID,
    user: CurrentUser,
    roles: CurrentRoles,
    _access: ModuleAccess,
    session: Session,
) -> None:
    try:
        comparison = await service.get(session, comparison_id)
        await service.delete(
            session, comparison, actor=user, is_admin="super_admin" in roles
        )
    except ComparisonError as exc:
        raise _translate(exc) from exc


def _out(comparison) -> ComparisonOut:
    body = ComparisonOut.model_validate(comparison)
    body.created_by_name = (
        comparison.created_by.display_name if comparison.created_by else None
    )
    for quote, row in zip(body.quotes, comparison.quotes, strict=True):
        if row.file_bytes:
            quote.document_url = (
                f"/api/v1/comparisons/{comparison.id}/quotes/{row.id}/document"
            )
    return body
