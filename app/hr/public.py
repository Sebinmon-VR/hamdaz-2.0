"""The candidate-facing side. No authentication, and no way into the ERP.

This router is mounted **outside** ``settings.api_prefix``, at ``/apply`` and
``/careers``. That is not cosmetic. Everything under the API prefix is written
on the assumption that a session cookie may be present and that the caller is a
colleague; nothing here may assume either, so it lives somewhere else entirely
and shares no dependency with it.

What that separation buys, concretely:

* **No authentication dependency is imported.** Not ``current_user``, not
  ``CurrentRoles``. There is no code path from a request here to a session, so
  no cookie can be read and none is ever set.
* **No internal identifier leaves.** Openings are found by share token only —
  never by id or slug — and the response models in ``schemas`` carry no ids at
  all. A candidate cannot learn an opening's id, a template's id, or that
  either exists.
* **No link back.** The hosted page contains no URL belonging to the ERP, loads
  nothing from it, and sends ``Referrer-Policy: no-referrer`` so the ERP's own
  origin is not even disclosed to anywhere the candidate goes next.
* **CORS without credentials.** These responses say
  ``Access-Control-Allow-Origin: *`` and deliberately do not say
  ``Allow-Credentials``, so a browser will neither attach an ERP cookie to
  these requests nor expose the response to a page that tried.

The one thing a token holder can do is read a posted opening and apply to it.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import status as http_status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

# Starlette's, deliberately, not FastAPI's. The multipart parser builds
# starlette.datastructures.UploadFile; fastapi.UploadFile is a subclass of
# it, so an isinstance check against the subclass silently matches nothing
# and every uploaded file is read back as the string "UploadFile(...)".
from starlette.datastructures import UploadFile

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.hr import render, service
from app.hr.documents import MAX_FILE_BYTES, UploadError, accept
from app.hr.schemas import (
    PublicDetailOut,
    PublicFieldOut,
    PublicListingOut,
    PublicOpeningOut,
    PublicSectionOut,
    PublicSubmitOut,
)
from app.models.hr import JobOpening
from app.models.templates import FieldType, FormTemplate

logger = logging.getLogger("hamdaz.hr.public")

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]

#: At most this many files on one application, whatever the form asks for.
MAX_UPLOADS: Final[int] = 5

#: Headers on everything this router serves. The CSP is the important one: the
#: hosted page is entirely self-contained, so anything it could be made to load
#: from elsewhere is an injection rather than a feature.
_SECURITY_HEADERS: Final[dict[str, str]] = {
    "Access-Control-Allow-Origin": "*",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "script-src 'unsafe-inline'; form-action 'self'; connect-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


def _harden(response: Response) -> None:
    """Apply the headers above without stamping on the CORS middleware.

    Where the request came from an allowlisted ERP origin the middleware sets
    its own, more specific ``Access-Control-Allow-Origin`` after this runs, and
    a plain assignment there replaces rather than appends.
    """
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value


router = APIRouter(tags=["careers (public)"], include_in_schema=True)


# ── a brake on repeat submissions ──────────────────────────────────────
#
# In-process and best effort, and said so out loud in the setting's comment. It
# stops the same person submitting the same form fifty times; it is not a
# defence against anything distributed, and it forgets everything on restart.

_recent: dict[str, deque[float]] = {}


def _rate_limited(key: str, *, limit: int, window: float = 3600.0) -> bool:
    now = time.monotonic()
    seen = _recent.setdefault(key, deque())
    while seen and now - seen[0] > window:
        seen.popleft()
    if len(seen) >= limit:
        return True
    seen.append(now)
    if len(_recent) > 10_000:
        # Unbounded growth on a public endpoint is a slow memory leak with a
        # public trigger. Drop the coldest entries rather than grow for ever.
        for stale in [k for k, v in _recent.items() if not v or now - v[-1] > window][:5_000]:
            _recent.pop(stale, None)
    return False


def _client_key(request: Request, token: str) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    client = forwarded or (request.client.host if request.client else "unknown")
    return f"{token[:12]}:{client}"


# ── shaping what a candidate sees ──────────────────────────────────────


def _public_fields(template: FormTemplate) -> tuple[list[PublicSectionOut], list[PublicFieldOut]]:
    """The questions, with everything internal removed.

    Built by naming what goes *out* rather than by deleting what should not:
    a field spec gains keys over time — ``scoring`` and ``maps_to`` already —
    and a blocklist is a list somebody forgets to add the next one to.
    """
    sections = [
        PublicSectionOut(
            key=str(s.get("key") or ""),
            name=str(s.get("name") or ""),
            help=s.get("help"),
        )
        for s in (template.sections or [])
        if isinstance(s, dict) and s.get("key")
    ]
    fields = [
        PublicFieldOut(
            key=str(f.get("key")),
            label=str(f.get("label") or f.get("key")),
            type=str(f.get("type") or FieldType.TEXT.value),
            section=f.get("section"),
            required=bool(f.get("required")),
            help=f.get("help"),
            options=f.get("options"),
            default=f.get("default"),
        )
        for f in (template.fields or [])
        if isinstance(f, dict) and f.get("key")
    ]
    return sections, fields


def _public_details(opening: JobOpening) -> list[PublicDetailOut]:
    """The advert, minus everything a candidate has no business reading.

    Driven off the posting *template*, not off the stored answers: iterating the
    answers would publish any key that ever got into the blob, including one
    left behind by a field since marked internal. The template is the statement
    of what is publishable, so it is what decides.
    """
    template = opening.posting_template
    if template is None:
        return []
    answers = opening.details or {}
    out: list[PublicDetailOut] = []
    for spec in template.fields or []:
        if not isinstance(spec, dict) or spec.get("internal"):
            continue
        key = str(spec.get("key") or "")
        # Skip the four already shown as fields of their own, so the advert
        # does not say the same thing twice.
        if not key or key in service.MIRRORED:
            continue
        value = answers.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        out.append(
            PublicDetailOut(key=key, label=str(spec.get("label") or key), value=value)
        )
    return out


async def _public_view(
    session: AsyncSession, opening: JobOpening, *, settings: Settings, request: Request
) -> PublicOpeningOut:
    template = await session.get(FormTemplate, opening.template_id)
    if template is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This form is temporarily unavailable. Please try again later.",
        )
    sections, fields = _public_fields(template)
    accepting = opening.accepts_applications
    base = settings.form_api_base(str(request.base_url))
    return PublicOpeningOut(
        title=opening.title,
        department=opening.department,
        location=opening.location,
        # str(), not .value: the column is a plain String, so a row read back
        # from Postgres carries a str rather than the enum member.
        employment_type=str(opening.employment_type).replace("_", " "),
        salary_range=opening.salary_range,
        summary=opening.summary,
        description=opening.description,
        requirements=opening.requirements,
        details=_public_details(opening),
        closes_on=opening.closes_on,
        accepting=accepting,
        closed_message=None if accepting else "This role is no longer accepting applications.",
        sections=sections,
        fields=fields,
        submit_url=f"{base}/apply/{opening.public_token}",
    )


async def _load(session: AsyncSession, token: str) -> JobOpening:
    try:
        return await service.by_token(session, token)
    except service.NotFoundError:
        # Deliberately the same 404 for a wrong token, a draft and a deleted
        # opening. Distinguishing them would confirm which tokens exist.
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="This link is not valid. Please check it with whoever sent it.",
        ) from None


# ── the hosted page ────────────────────────────────────────────────────


@router.get(
    "/apply/{token}",
    response_class=HTMLResponse,
    include_in_schema=False,
    summary="The application form, as a self-contained page",
)
async def hosted_form(
    token: str, request: Request, session: Session, settings: Config
) -> HTMLResponse:
    opening = await _load(session, token)
    if not opening.hosted_form:
        # HR turned the hosted page off because they run their own careers site.
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="This link is not valid. Please check it with whoever sent it.",
        )
    view = await _public_view(session, opening, settings=settings, request=request)
    response = HTMLResponse(render.application_page(view))
    _harden(response)
    return response


@router.get(
    "/apply/{token}/form",
    response_model=PublicOpeningOut,
    summary="The opening and its questions, as JSON",
)
async def form_json(
    token: str, request: Request, response: Response, session: Session, settings: Config
) -> PublicOpeningOut:
    """For an organisation rendering the form on its own careers site."""
    opening = await _load(session, token)
    _harden(response)
    return await _public_view(session, opening, settings=settings, request=request)


@router.post(
    "/apply/{token}",
    response_model=PublicSubmitOut,
    summary="Apply",
)
async def apply(
    token: str,
    request: Request,
    response: Response,
    session: Session,
    settings: Config,
) -> PublicSubmitOut:
    """Accept one application.

    The body is read off ``request.form()`` rather than declared as parameters.
    It has to be: the questions are a super admin's decision and change without
    a deploy, so there is no fixed signature to declare. Multipart is what both
    the hosted page and a custom careers site post, and it is a CORS-safelisted
    content type, so no preflight is needed from anywhere.
    """
    opening = await _load(session, token)
    _harden(response)

    if _rate_limited(_client_key(request, token), limit=settings.application_rate_limit):
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many submissions from here. Please try again later.",
        )

    form = await request.form()
    answers: dict[str, Any] = {}
    files: list[tuple[str | None, UploadFile]] = []
    # multi_items, not keys: a checkbox group posts one key several times, and
    # indexing the mapping would silently keep only the last one.
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            files.append((None if key == "files" else key, value))
        elif key in answers:
            # A repeated key is a multi-select, so it becomes a list rather
            # than the last value quietly winning.
            existing = answers[key]
            if not isinstance(existing, list):
                existing = [existing]
            answers[key] = [*existing, _coerce(str(value))]
        else:
            answers[key] = _coerce(str(value))

    if len(files) > MAX_UPLOADS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_UPLOADS} files, please.",
        )

    uploads: list[tuple[str | None, Any]] = []
    for field_key, upload in files:
        # One byte past the limit is enough to know it is over it, and stops a
        # 2GB upload being read into memory before it is refused.
        content = await upload.read(MAX_FILE_BYTES + 1)
        if not content:
            continue  # an empty file input, which browsers post anyway
        try:
            uploads.append(
                (field_key, accept(upload.filename or "file", content, upload.content_type))
            )
        except UploadError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    try:
        application = await service.submit_application(
            session, opening, answers=answers, uploads=uploads
        )
    except service.HRError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    # No explicit commit: get_session commits when the request succeeds, and a
    # second one here would only make the transaction boundary ambiguous.
    logger.info("public application on %s", opening.slug)
    return PublicSubmitOut(
        message=(
            f"Thank you. Your application for {opening.title} has been received. "
            f"We will be in touch by email."
        ),
        candidate_email=application.candidate_email,
    )


def _coerce(raw: str) -> Any:
    """Form values are strings. Give back the obvious non-string ones.

    Only the unambiguous cases — checkbox truthiness and plain numbers. Anything
    cleverer would guess wrong on a phone number with a leading zero or on a
    date, and the template already says what each field is.
    """
    text = raw.strip()
    if text in ("true", "on"):
        return True
    if text == "false":
        return False
    if text and (text.lstrip("-").replace(".", "", 1).isdigit()):
        try:
            return float(text) if "." in text else int(text)
        except ValueError:
            return text
    return text


# ── the public careers list ────────────────────────────────────────────


@router.get(
    "/careers",
    response_class=HTMLResponse,
    include_in_schema=False,
    summary="Open roles, as a page",
)
async def careers_page(request: Request, session: Session, settings: Config) -> HTMLResponse:
    listings = await _listings(session, settings=settings, request=request)
    response = HTMLResponse(render.careers_page(listings))
    _harden(response)
    return response


@router.get(
    "/careers/openings",
    response_model=list[PublicListingOut],
    summary="Open roles, as JSON",
)
async def careers_json(
    request: Request, response: Response, session: Session, settings: Config
) -> list[PublicListingOut]:
    _harden(response)
    return await _listings(session, settings=settings, request=request)


async def _listings(
    session: AsyncSession, *, settings: Settings, request: Request
) -> list[PublicListingOut]:
    """Only the openings HR marked as publicly listed.

    An opening left unlisted is not hidden by obscurity — it is not in this
    query at all, so nothing about it can be inferred from here.
    """
    base = settings.share_base(str(request.base_url))
    return [
        PublicListingOut(
            title=o.title,
            department=o.department,
            location=o.location,
            employment_type=str(o.employment_type).replace("_", " "),
            summary=o.summary,
            closes_on=o.closes_on,
            apply_url=f"{base}/apply/{o.public_token}",
        )
        for o in await service.listed_openings(session)
    ]
