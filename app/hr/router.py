"""HR: openings, applications, employee documents and performance reviews.

Everything here is authenticated and lives under the API prefix. The candidate
side is a different router at a different path with no authentication at all —
see ``app.hr.public``.

Most endpoints are HR only, and say so by depending on ``HRUser``. The
exceptions are the ones an ordinary colleague genuinely has business with, and
each is narrowed to their own row rather than to a filter they pass in:

* their own documents, and only the ones HR marked visible;
* the reviews they were nominated to write;
* reviews written about them, once HR has shared the cycle;
* their own performance summary.

A filter is not a permission. ``?user_id=`` on a shared endpoint is a request,
not a grant, so every one of those endpoints checks the row it loaded rather
than trusting the query it was asked for.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi import status as http_status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.forms import scoring
from app.forms.catalogue import JOB_APPLICATION, JOB_POSTING, PERFORMANCE_REVIEW
from app.hr import service
from app.hr.access import (
    HRUser,
    IsHR,
    PurgeAdmin,
    may_read_document,
    may_read_review,
    may_write_review,
)
from app.hr.documents import UploadError, accept, download_headers
from app.hr.schemas import (
    ApplicationOut,
    AttachmentOut,
    BulkNominateIn,
    CycleIn,
    CycleOut,
    DeclineIn,
    DocumentOut,
    DocumentUpdateIn,
    HireIn,
    NotesIn,
    OpeningIn,
    OpeningOut,
    OpeningSummaryOut,
    OpeningUpdateIn,
    PerformanceOut,
    RemovedOut,
    ReviewAnswersIn,
    ReviewOut,
    StageIn,
)
from app.models.hr import (
    ApplicationFile,
    ApplicationStage,
    DocumentKind,
    EmployeeDocument,
    EmploymentType,
    JobApplication,
    JobOpening,
    OpeningStatus,
    PerformanceReview,
    ReviewCycle,
    ReviewerRelation,
    ReviewStatus,
)
from app.models.templates import FormTemplate
from app.models.user import User

router = APIRouter(prefix="/hr", tags=["hr"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


def _translate(exc: service.HRError) -> HTTPException:
    if isinstance(exc, service.NotFoundError):
        return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=detail)


_NOUNS = {
    "openings": ("job opening", "job openings"),
    "applications": ("application", "applications"),
    "files": ("candidate file", "candidate files"),
    "documents": ("document", "documents"),
    "cycles": ("review cycle", "review cycles"),
    "reviews": ("review", "reviews"),
}


def _removed(removed: service.Removed) -> RemovedOut:
    """Turn the counts into a sentence, because a row of zeroes is not an answer."""
    counts = removed.as_dict()
    parts = [
        f"{n} {_NOUNS[key][0] if n == 1 else _NOUNS[key][1]}"
        for key, n in counts.items()
        if n
    ]
    if not parts:
        summary = "Nothing was deleted."
    elif len(parts) == 1:
        summary = f"Deleted {parts[0]}."
    else:
        summary = f"Deleted {', '.join(parts[:-1])} and {parts[-1]}."
    return RemovedOut(**counts, summary=summary)


# ── what the module is made of ─────────────────────────────────────────


@router.get("/meta", summary="The choices this module offers")
async def meta(_: CurrentUser, session: Session) -> dict[str, Any]:
    """Enumerations and template availability, so a frontend hardcodes none of it.

    Also reports whether the two forms HR depends on actually exist: without an
    active job application template there is nothing to post, and finding that
    out at the moment somebody clicks "post" is finding out too late.
    """
    async def forms(kind: str) -> dict[str, Any]:
        """Every active template of a kind, and which one is the default."""
        active = await service.templates_of(session, kind)
        default = await service.default_template(session, kind)
        return {
            "default_id": None if default is None else str(default.id),
            "templates": [
                {
                    "id": str(t.id),
                    "key": t.key,
                    "name": t.name,
                    "description": t.description,
                    "version": t.version,
                    "field_count": len(t.fields or []),
                    "tags": scoring.tags_of(t.fields or []),
                    "is_default": default is not None and t.id == default.id,
                }
                for t in active
            ],
        }

    return {
        "document_kinds": [k.value for k in DocumentKind],
        "employment_types": [t.value for t in EmploymentType],
        "application_stages": [s.value for s in ApplicationStage],
        "opening_statuses": [s.value for s in OpeningStatus],
        "reviewer_relations": [r.value for r in ReviewerRelation],
        # Every form HR may choose between, per kind. A single "the form"
        # would hide the variants a super admin went to the trouble of writing.
        "posting_forms": await forms(JOB_POSTING),
        "application_forms": await forms(JOB_APPLICATION),
        "review_forms": await forms(PERFORMANCE_REVIEW),
    }


# ── openings ───────────────────────────────────────────────────────────


def _opening_out(
    opening: JobOpening,
    *,
    settings: Settings,
    request: Request,
    applications: int = 0,
    new_applications: int = 0,
) -> OpeningOut:
    body = OpeningOut.model_validate(opening)
    body.team_name = opening.team.name if opening.team else None
    body.template_name = opening.template.name if opening.template else None
    if opening.posting_template is not None:
        body.posting_template_name = opening.posting_template.name
        body.posting_fields = list(opening.posting_template.fields or [])
        body.posting_sections = list(opening.posting_template.sections or [])
    body.created_by_name = opening.created_by.display_name if opening.created_by else None
    body.accepts_applications = opening.accepts_applications
    body.application_count = applications
    body.new_application_count = new_applications

    # Only for a posted opening. A share link to a draft goes nowhere, and
    # showing one is an invitation to send it before the job is real.
    if opening.status != OpeningStatus.DRAFT:
        share = settings.share_base(str(request.base_url))
        api = settings.form_api_base(str(request.base_url))
        body.share_url = f"{share}/apply/{opening.public_token}"
        body.share_api_url = f"{api}/apply/{opening.public_token}/form"
    return body


@router.get("/openings", response_model=list[OpeningSummaryOut], summary="Every opening")
async def list_openings(
    _: HRUser,
    session: Session,
    status: Annotated[OpeningStatus | None, Query()] = None,
) -> list[OpeningSummaryOut]:
    return [
        OpeningSummaryOut(
            id=o.id,
            title=o.title,
            slug=o.slug,
            status=o.status,
            department=o.department,
            location=o.location,
            employment_type=o.employment_type,
            publicly_listed=o.publicly_listed,
            posted_at=o.posted_at,
            closes_on=o.closes_on,
            application_count=total,
            new_application_count=new,
        )
        for o, total, new in await service.list_openings(session, status=status)
    ]


@router.post(
    "/openings",
    response_model=OpeningOut,
    status_code=http_status.HTTP_201_CREATED,
    summary="Create an opening",
)
async def create_opening(
    payload: OpeningIn, actor: HRUser, session: Session, settings: Config, request: Request
) -> OpeningOut:
    """Created as a draft. Posting it is a second, deliberate step."""
    try:
        opening = await service.create_opening(
            session, actor=actor, **payload.model_dump(exclude_unset=False)
        )
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _opening_out(opening, settings=settings, request=request)


@router.get("/openings/{ref}", response_model=OpeningOut, summary="One opening")
async def get_opening(
    ref: str, _: HRUser, session: Session, settings: Config, request: Request
) -> OpeningOut:
    try:
        opening = await service.get_opening(session, ref)
    except service.HRError as exc:
        raise _translate(exc) from exc
    counts = await session.execute(
        select(
            func.count(JobApplication.id),
            func.count(JobApplication.id).filter(JobApplication.stage == ApplicationStage.NEW),
        ).where(JobApplication.opening_id == opening.id)
    )
    total, new = counts.one()
    return _opening_out(
        opening, settings=settings, request=request, applications=total, new_applications=new
    )


@router.patch("/openings/{ref}", response_model=OpeningOut, summary="Edit an opening")
async def update_opening(
    ref: str,
    payload: OpeningUpdateIn,
    actor: HRUser,
    session: Session,
    settings: Config,
    request: Request,
) -> OpeningOut:
    try:
        opening = await service.get_opening(session, ref)
        opening = await service.update_opening(
            session, opening, actor=actor, **payload.model_dump(exclude_unset=True)
        )
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _opening_out(opening, settings=settings, request=request)


@router.post("/openings/{ref}/post", response_model=OpeningOut, summary="Post it")
async def post_opening(
    ref: str, actor: HRUser, session: Session, settings: Config, request: Request
) -> OpeningOut:
    """Makes the share link live and starts accepting applications."""
    try:
        opening = await service.get_opening(session, ref)
        opening = await service.post_opening(session, opening, actor=actor)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _opening_out(opening, settings=settings, request=request)


@router.post("/openings/{ref}/close", response_model=OpeningOut, summary="Stop accepting")
async def close_opening(
    ref: str,
    actor: HRUser,
    session: Session,
    settings: Config,
    request: Request,
    filled: Annotated[bool, Query(description="Closed because somebody was hired")] = False,
) -> OpeningOut:
    try:
        opening = await service.get_opening(session, ref)
        opening = await service.close_opening(session, opening, actor=actor, filled=filled)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _opening_out(opening, settings=settings, request=request)


@router.post(
    "/openings/{ref}/rotate-link",
    response_model=OpeningOut,
    summary="Issue a new share link",
)
async def rotate_link(
    ref: str, actor: HRUser, session: Session, settings: Config, request: Request
) -> OpeningOut:
    """Revokes the old link immediately.

    Everybody holding the previous URL — including candidates part way through
    the form — loses it. That is the point of the endpoint, and worth saying in
    the UI before the button is pressed.
    """
    try:
        opening = await service.get_opening(session, ref)
        opening = await service.rotate_token(session, opening, actor=actor)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _opening_out(opening, settings=settings, request=request)


@router.delete(
    "/openings/{ref}",
    response_model=RemovedOut,
    summary="Delete an opening and everything sent to it (super admin)",
)
async def delete_opening(ref: str, actor: PurgeAdmin, session: Session) -> RemovedOut:
    """Destroys the opening, every application to it, and their files.

    Not recoverable. Offer letters already filed against an employee survive —
    their link to the application is severed rather than followed — so deleting
    the opening somebody was hired through never destroys their contract.

    Closing an opening is almost always what is wanted instead: it stops the
    link accepting applications and keeps the record.
    """
    try:
        opening = await service.get_opening(session, ref)
        removed = await service.delete_opening(session, opening, actor=actor)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _removed(removed)


# ── applications ───────────────────────────────────────────────────────


def _application_out(application: JobApplication) -> ApplicationOut:
    body = ApplicationOut.model_validate(application)
    body.opening_title = application.opening.title if application.opening else None
    body.decided_by_name = (
        application.decided_by.display_name if application.decided_by else None
    )
    body.attachments = [AttachmentOut.model_validate(a) for a in application.attachments]
    return body


@router.get("/applications", response_model=list[ApplicationOut], summary="Applications")
async def list_applications(
    _: HRUser,
    session: Session,
    opening_id: Annotated[uuid.UUID | None, Query()] = None,
    stage: Annotated[ApplicationStage | None, Query()] = None,
) -> list[ApplicationOut]:
    """Highest scoring first, then most recent.

    The order is a starting point for reading a pile, not a ranking to act on:
    a form scores what it can measure, which is never the whole of a candidate.
    """
    return [
        _application_out(a)
        for a in await service.list_applications(session, opening_id=opening_id, stage=stage)
    ]


@router.get(
    "/applications/{application_id}",
    response_model=ApplicationOut,
    summary="One application",
)
async def get_application(
    application_id: uuid.UUID, _: HRUser, session: Session
) -> ApplicationOut:
    try:
        application = await service.get_application(session, application_id)
    except service.HRError as exc:
        raise _translate(exc) from exc
    await session.refresh(application, ["opening"])
    return _application_out(application)


@router.post(
    "/applications/{application_id}/stage",
    response_model=ApplicationOut,
    summary="Move a candidate along",
)
async def move_stage(
    application_id: uuid.UUID, payload: StageIn, actor: HRUser, session: Session
) -> ApplicationOut:
    try:
        application = await service.get_application(session, application_id)
        await service.move_stage(
            session, application, actor=actor, stage=payload.stage, note=payload.note
        )
    except service.HRError as exc:
        raise _translate(exc) from exc
    await session.refresh(application, ["opening"])
    return _application_out(application)


@router.patch(
    "/applications/{application_id}/notes",
    response_model=ApplicationOut,
    summary="HR's own notes",
)
async def set_notes(
    application_id: uuid.UUID, payload: NotesIn, _: HRUser, session: Session
) -> ApplicationOut:
    try:
        application = await service.get_application(session, application_id)
    except service.HRError as exc:
        raise _translate(exc) from exc
    application.internal_notes = payload.internal_notes
    await session.flush()
    await session.refresh(application, ["opening"])
    return _application_out(application)


@router.post(
    "/applications/{application_id}/hire",
    response_model=ApplicationOut,
    summary="Record who they became",
)
async def hire(
    application_id: uuid.UUID, payload: HireIn, actor: HRUser, session: Session
) -> ApplicationOut:
    try:
        application = await service.get_application(session, application_id)
        await service.hire(
            session,
            application,
            actor=actor,
            user_id=payload.user_id,
            close_opening_too=payload.close_opening,
        )
    except service.HRError as exc:
        raise _translate(exc) from exc
    await session.refresh(application, ["opening"])
    return _application_out(application)


@router.delete(
    "/applications/{application_id}",
    response_model=RemovedOut,
    summary="Delete one candidate's application (super admin)",
)
async def delete_application(
    application_id: uuid.UUID, actor: PurgeAdmin, session: Session
) -> RemovedOut:
    """Removes the application and the documents the candidate uploaded.

    This is the endpoint behind a candidate asking to be forgotten, which is why
    it takes their files with it rather than leaving orphaned CVs behind.
    """
    try:
        application = await service.get_application(session, application_id)
        removed = await service.delete_application(session, application, actor=actor)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _removed(removed)


@router.get(
    "/applications/{application_id}/files/{file_id}",
    summary="Download a candidate's file",
    response_class=Response,
)
async def download_attachment(
    application_id: uuid.UUID, file_id: uuid.UUID, _: HRUser, session: Session
) -> Response:
    attachment = await session.get(ApplicationFile, file_id)
    # Checked against the application in the path as well as by id: an id on its
    # own would let a mistyped URL hand back a file from another candidate.
    if attachment is None or attachment.application_id != application_id:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="No such file"
        )
    return Response(
        content=attachment.file_bytes,
        media_type=attachment.content_type or "application/octet-stream",
        headers=download_headers(attachment.file_name),
    )


# ── employee documents ─────────────────────────────────────────────────


def _document_out(document: EmployeeDocument) -> DocumentOut:
    body = DocumentOut.model_validate(document)
    body.user_name = document.user.display_name if document.user else None
    body.uploaded_by_name = (
        document.uploaded_by.display_name if document.uploaded_by else None
    )
    body.expired = bool(document.expires_on and document.expires_on < date.today())
    return body


@router.get("/me/documents", response_model=list[DocumentOut], summary="My own documents")
async def my_documents(user: CurrentUser, session: Session) -> list[DocumentOut]:
    """Everything HR has filed about the caller and marked as theirs to see."""
    return [
        _document_out(d)
        for d in await service.list_documents(session, user_id=user.id)
        if d.visible_to_employee
    ]


@router.get("/documents", response_model=list[DocumentOut], summary="Documents")
async def list_documents(
    user: CurrentUser,
    hr: IsHR,
    session: Session,
    user_id: Annotated[uuid.UUID | None, Query()] = None,
    expiring_within_days: Annotated[
        int | None,
        Query(ge=1, le=3650, description="Only documents expiring within this many days"),
    ] = None,
) -> list[DocumentOut]:
    if not hr:
        # Not a 403 on the endpoint: a colleague asking for their own documents
        # is a reasonable request, and answering it here saves them knowing
        # about a second URL. Anything else is refused.
        if user_id not in (None, user.id):
            raise _forbidden("You can only see your own documents.")
        return await my_documents(user, session)
    return [
        _document_out(d)
        for d in await service.list_documents(
            session, user_id=user_id, expiring_within_days=expiring_within_days
        )
    ]


@router.post(
    "/people/{user_id}/documents",
    response_model=DocumentOut,
    status_code=http_status.HTTP_201_CREATED,
    summary="File a document against somebody",
)
async def upload_document(
    user_id: uuid.UUID,
    actor: HRUser,
    session: Session,
    file: Annotated[UploadFile, File(description="PDF, Word, Excel, image or text")],
    kind: Annotated[DocumentKind, Form()] = DocumentKind.OTHER,
    title: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
    issued_on: Annotated[date | None, Form()] = None,
    expires_on: Annotated[date | None, Form()] = None,
    visible_to_employee: Annotated[bool, Form()] = True,
    source_application_id: Annotated[uuid.UUID | None, Form()] = None,
) -> DocumentOut:
    """An offer letter, a contract, a visa — ``kind`` says which.

    ``kind`` is a label, not a schema: every kind is stored the same way, so HR
    is never blocked on a deploy to file something the list does not name.
    """
    try:
        upload = accept(file.filename or "file", await file.read(), file.content_type)
    except UploadError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    try:
        document = await service.add_document(
            session,
            actor=actor,
            user_id=user_id,
            upload=upload,
            kind=kind,
            title=title,
            note=note,
            issued_on=issued_on,
            expires_on=expires_on,
            visible_to_employee=visible_to_employee,
            source_application_id=source_application_id,
        )
    except service.HRError as exc:
        raise _translate(exc) from exc
    await session.refresh(document, ["user", "uploaded_by"])
    return _document_out(document)


@router.delete(
    "/people/{user_id}/hr-data",
    response_model=RemovedOut,
    summary="Delete everything HR holds about one person (super admin)",
)
async def purge_person(
    user_id: uuid.UUID, actor: PurgeAdmin, session: Session
) -> RemovedOut:
    """Their documents and every review written about them.

    Reviews they *wrote* about colleagues are left alone: those are records
    about somebody else, and erasing a leaver should not quietly remove half the
    evidence behind another person's appraisal.

    The user row itself is not touched. Deactivating somebody is the teams
    module's business, and this endpoint deliberately does not reach into it.
    """
    try:
        removed = await service.purge_person(session, user_id, actor=actor)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _removed(removed)


@router.get("/documents/{document_id}", response_model=DocumentOut, summary="One document")
async def get_document(
    document_id: uuid.UUID, user: CurrentUser, hr: IsHR, session: Session
) -> DocumentOut:
    try:
        document = await service.get_document(session, document_id)
    except service.HRError as exc:
        raise _translate(exc) from exc
    if not may_read_document(document, viewer_id=user.id, hr=hr):
        # 404, not 403. Confirming that a document exists on somebody is itself
        # something a colleague should not learn.
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="No such document")
    return _document_out(document)


@router.get(
    "/documents/{document_id}/download",
    summary="Download a document",
    response_class=Response,
)
async def download_document(
    document_id: uuid.UUID, user: CurrentUser, hr: IsHR, session: Session
) -> Response:
    try:
        document = await service.get_document(session, document_id)
    except service.HRError as exc:
        raise _translate(exc) from exc
    if not may_read_document(document, viewer_id=user.id, hr=hr):
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="No such document")
    return Response(
        content=document.file_bytes,
        media_type=document.content_type or "application/octet-stream",
        headers=download_headers(document.file_name),
    )


@router.patch("/documents/{document_id}", response_model=DocumentOut, summary="Edit the details")
async def update_document(
    document_id: uuid.UUID, payload: DocumentUpdateIn, _: HRUser, session: Session
) -> DocumentOut:
    try:
        document = await service.get_document(session, document_id)
        document = await service.update_document(
            session, document, **payload.model_dump(exclude_unset=True)
        )
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _document_out(document)


@router.delete(
    "/documents/{document_id}",
    response_model=RemovedOut,
    summary="Delete a document (super admin)",
)
async def delete_document(
    document_id: uuid.UUID, actor: PurgeAdmin, session: Session
) -> RemovedOut:
    """HR files documents; only a super admin removes one.

    The asymmetry is the point. Uploading a contract is routine and belongs to
    the team that does the work; destroying one is not, and the file is often
    the only copy anybody can produce later.
    """
    try:
        document = await service.get_document(session, document_id)
        removed = await service.delete_document(session, document, actor=actor)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _removed(removed)


# ── review cycles ──────────────────────────────────────────────────────


async def _cycle_out(session: AsyncSession, cycle: ReviewCycle) -> CycleOut:
    body = CycleOut.model_validate(cycle)
    body.template_name = cycle.template.name if cycle.template else None
    body.created_by_name = cycle.created_by.display_name if cycle.created_by else None
    counts = await session.execute(
        select(
            func.count(PerformanceReview.id),
            func.count(PerformanceReview.id).filter(
                PerformanceReview.status == ReviewStatus.SUBMITTED
            ),
            func.count(func.distinct(PerformanceReview.subject_id)),
        ).where(PerformanceReview.cycle_id == cycle.id)
    )
    body.nominated, body.submitted, body.subjects = counts.one()
    return body


@router.get("/review-cycles", response_model=list[CycleOut], summary="Review cycles")
async def list_cycles(_: HRUser, session: Session) -> list[CycleOut]:
    return [await _cycle_out(session, c) for c in await service.list_cycles(session)]


@router.post(
    "/review-cycles",
    response_model=CycleOut,
    status_code=http_status.HTTP_201_CREATED,
    summary="Start a review cycle",
)
async def create_cycle(payload: CycleIn, actor: HRUser, session: Session) -> CycleOut:
    """Created as a draft, with nobody nominated. Both are deliberate steps."""
    try:
        cycle = await service.create_cycle(session, actor=actor, **payload.model_dump())
    except service.HRError as exc:
        raise _translate(exc) from exc
    return await _cycle_out(session, cycle)


@router.get("/review-cycles/{cycle_id}", response_model=CycleOut, summary="One cycle")
async def get_cycle(cycle_id: uuid.UUID, _: HRUser, session: Session) -> CycleOut:
    try:
        cycle = await service.get_cycle(session, cycle_id)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return await _cycle_out(session, cycle)


@router.post("/review-cycles/{cycle_id}/open", response_model=CycleOut, summary="Open it")
async def open_cycle(cycle_id: uuid.UUID, _: HRUser, session: Session) -> CycleOut:
    """Lets the nominated reviewers start filling their forms in."""
    try:
        cycle = await service.get_cycle(session, cycle_id)
        cycle = await service.open_cycle(session, cycle)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return await _cycle_out(session, cycle)


@router.post("/review-cycles/{cycle_id}/close", response_model=CycleOut, summary="Close it")
async def close_cycle(cycle_id: uuid.UUID, _: HRUser, session: Session) -> CycleOut:
    try:
        cycle = await service.get_cycle(session, cycle_id)
        cycle = await service.close_cycle(session, cycle)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return await _cycle_out(session, cycle)


@router.post(
    "/review-cycles/{cycle_id}/sharing",
    response_model=CycleOut,
    summary="Share the results with the people reviewed",
)
async def set_sharing(
    cycle_id: uuid.UUID,
    _: HRUser,
    session: Session,
    shared: Annotated[bool, Query(description="Whether subjects may read their reviews")],
) -> CycleOut:
    try:
        cycle = await service.get_cycle(session, cycle_id)
        cycle = await service.set_cycle_sharing(session, cycle, shared=shared)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return await _cycle_out(session, cycle)


@router.delete(
    "/review-cycles/{cycle_id}",
    response_model=RemovedOut,
    summary="Delete a cycle and every review in it (super admin)",
)
async def delete_cycle(
    cycle_id: uuid.UUID, actor: PurgeAdmin, session: Session
) -> RemovedOut:
    """Destroys every assessment written in this cycle, submitted ones included.

    Closing the cycle is what is wanted almost every time: it stops anybody
    submitting and leaves the scores readable.
    """
    try:
        cycle = await service.get_cycle(session, cycle_id)
        removed = await service.delete_cycle(session, cycle, actor=actor)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _removed(removed)


@router.post(
    "/review-cycles/{cycle_id}/nominations",
    response_model=list[ReviewOut],
    status_code=http_status.HTTP_201_CREATED,
    summary="Nominate reviewers",
)
async def nominate(
    cycle_id: uuid.UUID, payload: BulkNominateIn, _: HRUser, session: Session
) -> list[ReviewOut]:
    """Ask people to review people. A person reviewing themselves is allowed.

    All or nothing: one bad nomination fails the whole request rather than
    leaving HR to work out which half of a list of forty went in.
    """
    try:
        cycle = await service.get_cycle(session, cycle_id)
        created = [
            await service.nominate(
                session,
                cycle,
                subject_id=n.subject_id,
                reviewer_id=n.reviewer_id,
                relation=n.relation,
                due_on=n.due_on,
            )
            for n in payload.nominations
        ]
    except service.HRError as exc:
        raise _translate(exc) from exc
    return [_review_out(r, include_content=True) for r in created]


@router.post(
    "/reviews/{review_id}/withdraw",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Withdraw a nomination that has not been started",
)
async def withdraw(review_id: uuid.UUID, _: HRUser, session: Session) -> None:
    """The ordinary HR correction: the wrong person was nominated.

    Refused the moment that reviewer has written anything, because at that point
    it stops being an un-invitation and starts being a deletion — which is a
    super admin's call, through DELETE below.
    """
    try:
        review = await service.get_review(session, review_id)
        await service.withdraw_nomination(session, review)
    except service.HRError as exc:
        raise _translate(exc) from exc


@router.delete(
    "/reviews/{review_id}",
    response_model=RemovedOut,
    summary="Delete a review, whatever state it is in (super admin)",
)
async def delete_review(
    review_id: uuid.UUID, actor: PurgeAdmin, session: Session
) -> RemovedOut:
    try:
        review = await service.get_review(session, review_id)
        removed = await service.delete_review(session, review, actor=actor)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _removed(removed)


# ── writing and reading reviews ────────────────────────────────────────


def _review_out(
    review: PerformanceReview,
    *,
    include_content: bool,
    template: FormTemplate | None = None,
) -> ReviewOut:
    """One review. ``include_content`` decides whether the answers come with it.

    A subject can always see *that* they are being reviewed and by whom — that
    is not a secret worth keeping and knowing it is how somebody chases their
    own review. What was written is a separate question, answered by
    ``app.hr.access.may_read_review``.
    """
    body = ReviewOut.model_validate(review, from_attributes=True)
    body.cycle_name = review.cycle.name if review.cycle else None
    body.subject_name = review.subject.display_name if review.subject else None
    body.reviewer_name = review.reviewer.display_name if review.reviewer else None
    if not include_content:
        body.answers = body.score = None
        body.score_percent = None
        body.comment = body.declined_reason = None
    if template is not None:
        body.fields = list(template.fields or [])
        body.sections = list(template.sections or [])
    return body


@router.get("/reviews/mine", response_model=list[ReviewOut], summary="Reviews I have to write")
async def my_reviews(user: CurrentUser, session: Session) -> list[ReviewOut]:
    """What the caller has been nominated for, across every cycle."""
    return [
        _review_out(r, include_content=True)
        for r in await service.list_reviews(session, reviewer_id=user.id)
    ]


@router.get("/reviews/about-me", response_model=list[ReviewOut], summary="Reviews about me")
async def reviews_about_me(user: CurrentUser, session: Session) -> list[ReviewOut]:
    """Who is reviewing the caller, and what they said once HR has shared it."""
    return [
        _review_out(
            r, include_content=may_read_review(r, viewer_id=user.id, hr=False)
        )
        for r in await service.list_reviews(session, subject_id=user.id)
    ]


@router.get("/reviews", response_model=list[ReviewOut], summary="Reviews")
async def list_reviews(
    user: CurrentUser,
    hr: IsHR,
    session: Session,
    cycle_id: Annotated[uuid.UUID | None, Query()] = None,
    subject_id: Annotated[uuid.UUID | None, Query()] = None,
    reviewer_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[ReviewStatus | None, Query()] = None,
) -> list[ReviewOut]:
    if not hr:
        # Narrowed to the caller rather than refused: the filters they are
        # allowed are the ones about themselves.
        if subject_id not in (None, user.id) and reviewer_id not in (None, user.id):
            raise _forbidden("You can only see reviews you wrote or that are about you.")
        rows = await service.list_reviews(
            session, cycle_id=cycle_id, reviewer_id=user.id, status=status
        )
        rows += [
            r
            for r in await service.list_reviews(
                session, cycle_id=cycle_id, subject_id=user.id, status=status
            )
            if r.reviewer_id != user.id
        ]
        return [
            _review_out(r, include_content=may_read_review(r, viewer_id=user.id, hr=False))
            for r in rows
        ]

    return [
        _review_out(r, include_content=True)
        for r in await service.list_reviews(
            session,
            cycle_id=cycle_id,
            subject_id=subject_id,
            reviewer_id=reviewer_id,
            status=status,
        )
    ]


@router.get("/reviews/{review_id}", response_model=ReviewOut, summary="One review, with its form")
async def get_review(
    review_id: uuid.UUID, user: CurrentUser, hr: IsHR, session: Session
) -> ReviewOut:
    """The reviewer gets the questions with it, so the form and answers arrive together."""
    try:
        review = await service.get_review(session, review_id)
    except service.HRError as exc:
        raise _translate(exc) from exc
    if not may_read_review(review, viewer_id=user.id, hr=hr):
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="No such review")

    template = (
        await session.get(FormTemplate, review.cycle.template_id)
        if review.reviewer_id == user.id or hr
        else None
    )
    return _review_out(review, include_content=True, template=template)


@router.put("/reviews/{review_id}", response_model=ReviewOut, summary="Save or submit a review")
async def write_review(
    review_id: uuid.UUID, payload: ReviewAnswersIn, user: CurrentUser, session: Session
) -> ReviewOut:
    """Only the nominated reviewer, and only while the cycle is open.

    HR cannot write here even though it can read everything: an assessment
    attributed to somebody who did not write it is evidence about nobody.
    """
    try:
        review = await service.get_review(session, review_id)
    except service.HRError as exc:
        raise _translate(exc) from exc

    allowed, why = may_write_review(review, viewer_id=user.id)
    if not allowed:
        raise _forbidden(why)

    try:
        review = await service.save_review(
            session,
            review,
            answers=payload.answers,
            comment=payload.comment,
            submit=payload.submit,
        )
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _review_out(review, include_content=True)


@router.post("/reviews/{review_id}/decline", response_model=ReviewOut, summary="Decline to review")
async def decline(
    review_id: uuid.UUID, payload: DeclineIn, user: CurrentUser, session: Session
) -> ReviewOut:
    try:
        review = await service.get_review(session, review_id)
    except service.HRError as exc:
        raise _translate(exc) from exc
    if review.reviewer_id != user.id:
        raise _forbidden("Only the nominated reviewer can decline.")
    review = await service.decline_review(session, review, reason=payload.reason)
    return _review_out(review, include_content=True)


@router.post("/reviews/{review_id}/reopen", response_model=ReviewOut, summary="Hand one back")
async def reopen(review_id: uuid.UUID, _: HRUser, session: Session) -> ReviewOut:
    """Returns a submitted review to its author, clearing the frozen score."""
    try:
        review = await service.get_review(session, review_id)
        review = await service.reopen_review(session, review)
    except service.HRError as exc:
        raise _translate(exc) from exc
    return _review_out(review, include_content=True)


# ── the performance picture ────────────────────────────────────────────


@router.get("/me/performance", response_model=PerformanceOut, summary="My performance")
async def my_performance(user: CurrentUser, session: Session) -> PerformanceOut:
    """The caller's own combined score. Answers only, never who said what.

    Deliberately not gated on the cycle being shared: a person is entitled to
    the aggregate about themselves. What ``shared_with_subjects`` controls is
    reading an individual colleague's review, which is a different thing.
    """
    return PerformanceOut(
        user_name=user.display_name, **await service.performance_for(session, user.id)
    )


@router.get(
    "/people/{user_id}/performance",
    response_model=PerformanceOut,
    summary="Somebody's performance",
)
async def performance(
    user_id: uuid.UUID,
    user: CurrentUser,
    hr: IsHR,
    session: Session,
    cycle_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PerformanceOut:
    if not hr and user_id != user.id:
        raise _forbidden("Only HR can see somebody else's performance.")
    subject = await session.get(User, user_id)
    if subject is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="No such person")
    return PerformanceOut(
        user_name=subject.display_name,
        **await service.performance_for(session, user_id, cycle_id=cycle_id),
    )
