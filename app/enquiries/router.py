"""Enquiry analysis: a pre-sales task's documents, read and checked against our history.

Everything is addressed by the Proposals task id, because that is what a
person starts from: open a task, analyse its enquiry. The analysis row is made
on first use.

Gated by the ``enquiry_analysis`` module grant. What it shows — our past buy
and sell rates — is the same standing as the quote requests module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import module_guard
from app.auth.deps import CurrentUser
from app.core.config import get_settings
from app.core.db import get_session, get_session_factory
from app.enquiries import report, service
from app.enquiries.schemas import (
    AnalysisOut,
    DocumentOut,
    LineOut,
    ListOut,
    RunIn,
    SummaryOut,
    TaskAnalysisOut,
    TaskOut,
)
from app.enquiries.service import Deps, EnquiryError
from app.models.enquiry import EnquiryAnalysis

logger = logging.getLogger("hamdaz.enquiries")

MODULE_KEY = "enquiry_analysis"

router = APIRouter(
    prefix="/enquiries",
    tags=["enquiry analysis"],
    dependencies=[Depends(module_guard(MODULE_KEY, "Enquiry Analysis"))],
)

Session = Annotated[AsyncSession, Depends(get_session)]

#: Files per upload. A tender pack is a handful; more is a folder to sort first.
MAX_UPLOADS = 15
MAX_UPLOAD_BYTES = 60 * 1024 * 1024


def get_deps(request: Request) -> Deps:
    state = request.app.state
    return Deps(
        settings=get_settings(),
        factory=get_session_factory(),
        sharepoint=state.sharepoint,
        drive=state.quote_drive,
        extractor=state.quote_extractor,
        llm=state.enquiry_llm,
        zoho=state.zoho,
    )


Dependencies = Annotated[Deps, Depends(get_deps)]


def _translate(exc: EnquiryError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _out(analysis: EnquiryAnalysis) -> AnalysisOut:
    return AnalysisOut.model_validate(
        {
            **{k: getattr(analysis, k) for k in AnalysisOut.model_fields if hasattr(analysis, k)
               and k not in ("documents", "lines", "counts", "run_by_name")},
            "run_by_name": analysis.run_by.display_name if analysis.run_by else None,
            "counts": report.counts(analysis),
            "documents": [DocumentOut.model_validate(d) for d in analysis.documents],
            "lines": [LineOut.model_validate(line) for line in analysis.lines],
        }
    )


async def _load(session: AsyncSession, task_id: str) -> EnquiryAnalysis | None:
    analysis = await service.for_task(session, task_id)
    if analysis is not None and service.settle_orphan(analysis):
        await session.commit()
    return analysis


@router.get("", response_model=ListOut)
async def index(session: Session, _user: CurrentUser, limit: int = 200) -> ListOut:
    """Every analysed enquiry, the most recently run first."""
    rows = (
        await session.scalars(
            select(EnquiryAnalysis)
            .order_by(EnquiryAnalysis.finished_at.desc().nulls_last(), EnquiryAnalysis.created_at.desc())
            .limit(max(1, min(limit, 500)))
        )
    ).all()
    out = []
    for a in rows:
        c = report.counts(a)
        out.append(
            SummaryOut(
                task_id=a.task_id, task_title=a.task_title, end_user=a.end_user,
                bid_closing_date=a.bid_closing_date, status=a.status, items=len(a.lines),
                recent=c["recent"], history=c["history"], new=c["new"], finished_at=a.finished_at,
                run_by_name=a.run_by.display_name if a.run_by else None,
            )
        )
    return ListOut(analyses=out)


@router.get("/task/{task_id}", response_model=TaskAnalysisOut)
async def for_task(task_id: str, session: Session, _user: CurrentUser, deps: Dependencies) -> TaskAnalysisOut:
    """The task, and its analysis if one was ever run or uploaded to."""
    analysis = await _load(session, task_id)
    if analysis is not None:
        task = TaskOut(id=task_id, title=analysis.task_title, end_user=analysis.end_user,
                       bid_closing_date=analysis.bid_closing_date)
    else:
        try:
            facts = await service.task_facts(session, deps.sharepoint, task_id)
        except EnquiryError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        task = TaskOut(id=task_id, title=facts.title, end_user=facts.end_user,
                       bid_closing_date=facts.bid_closing_date)
    return TaskAnalysisOut(
        task=task,
        analysis=_out(analysis) if analysis is not None else None,
        can_run=deps.llm.configured,
        web_search_default=deps.settings.enquiry_web_search,
    )


@router.post("/task/{task_id}/run", response_model=AnalysisOut, status_code=status.HTTP_202_ACCEPTED)
async def run(
    task_id: str, body: RunIn, session: Session, user: CurrentUser, deps: Dependencies
) -> AnalysisOut:
    """Start (or re-run) the analysis. It runs in the background; poll the task."""
    if not deps.llm.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The analysis reads documents with Claude, and no ANTHROPIC_API_KEY is set.",
        )
    try:
        analysis = await service.get_or_create(session, deps.sharepoint, task_id, user)
    except EnquiryError as exc:
        raise _translate(exc) from exc
    service.settle_orphan(analysis)
    if analysis.status == "running":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This enquiry is being analysed already.")
    analysis.status, analysis.stage, analysis.error = "running", "Starting", None
    analysis.web_search = deps.settings.enquiry_web_search if body.web_search is None else body.web_search
    analysis.run_by = user
    await session.commit()
    service.start(deps, analysis.id, user.id)
    return _out(analysis)


@router.post("/task/{task_id}/documents", response_model=AnalysisOut)
async def upload(
    task_id: str,
    session: Session,
    user: CurrentUser,
    deps: Dependencies,
    files: Annotated[list[UploadFile], File(description="Tender, RFQ, BOQ, specs, supplier quotes")],
) -> AnalysisOut:
    """Save documents into the task's folder, for the next run to read."""
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Attach at least one file.")
    if len(files) > MAX_UPLOADS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"At most {MAX_UPLOADS} files at a time.")
    try:
        analysis = await service.get_or_create(session, deps.sharepoint, task_id, user)
        if analysis.status == "running" and not service.settle_orphan(analysis):
            raise EnquiryError("Wait for the running analysis to finish before adding documents.")
        payload = []
        for f in files:
            content = await f.read()
            if len(content) > MAX_UPLOAD_BYTES:
                raise EnquiryError(f"{f.filename} is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
            payload.append((f.filename or "document", content, f.content_type))
        await service.upload(session, deps, analysis, payload, user)
    except EnquiryError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return _out(analysis)


async def _done(session: AsyncSession, task_id: str) -> EnquiryAnalysis:
    analysis = await _load(session, task_id)
    # A finished run, or an earlier one's lines while a new run goes.
    if analysis is None or (analysis.status != "done" and not analysis.lines):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This enquiry has not been analysed yet.")
    return analysis


def _file(content: bytes, name: str, media_type: str) -> Response:
    ascii_name = name.encode("ascii", "ignore").decode() or "enquiry-analysis"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urlquote(name)}"
        },
    )


@router.get("/task/{task_id}/report.pdf", responses={200: {"content": {"application/pdf": {}}}})
async def report_pdf(task_id: str, session: Session, _user: CurrentUser) -> Response:
    analysis = await _done(session, task_id)
    content = await asyncio.to_thread(report.pdf, analysis)
    return _file(content, f"{report.file_stem(analysis)}.pdf", "application/pdf")


@router.get("/task/{task_id}/workbook.xlsx")
async def report_workbook(task_id: str, session: Session, _user: CurrentUser) -> Response:
    analysis = await _done(session, task_id)
    content = await asyncio.to_thread(report.workbook, analysis)
    return _file(
        content,
        f"{report.file_stem(analysis)}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
