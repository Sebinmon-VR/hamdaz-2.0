"""Which folder a quote's documents go into, and putting them there.

``storage.py`` knows about folders and files. This knows about quotes: which
task a quote came from, what its folder is called, what a document is named,
and the row that records where it went.

**A quote's folder is resolved once and remembered.** The first document filed
against a quote finds (or names) the task's folder and fixes
``request.drive_folder``; every later upload, and the report on submit, goes
to the same place. Resolving on every upload would move a quote's documents
between two folders the day somebody renamed the task.

**Uploads fail loudly; the report does not.** The library is the store, so a
document that cannot be filed is not attached — the person sees why and tries
again. The report is different: it is rendered at the moment the quote goes to
the approvers, and a quote that could not go up because a folder was locked
would be the worse failure. It is filed on a best effort and the failure is
written on the quote where the screen can show it.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.proposal_index import ProposalIndexItem
from app.models.quoting import DocumentKind, QuoteDocument, QuoteRequest
from app.models.user import User
from app.quoting import storage
from app.quoting.storage import DriveError, FolderMatch, QuoteDrive

logger = logging.getLogger("hamdaz.quoting")

#: How each kind reads as a file-name prefix and on screen.
KIND_LABELS: dict[str, str] = {
    DocumentKind.SUPPLIER_QUOTE: "Supplier quote",
    DocumentKind.CUSTOMER_RFQ: "Customer RFQ",
    DocumentKind.END_USER_PO: "End user PO",
    DocumentKind.TECHNICAL_SPEC: "Technical spec",
    DocumentKind.COMPLIANCE: "Compliance",
    DocumentKind.FREIGHT_QUOTE: "Freight quote",
    DocumentKind.COSTING_REPORT: "Costing report",
    DocumentKind.OTHER: "Document",
}


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(str(kind), "Document")


async def _task_title(session: AsyncSession, request: QuoteRequest) -> str | None:
    """The task's title as SharePoint has it now, from the mirror.

    The mirror rather than the live list: it is the same title, it is local,
    and the folder was named from it in the first place. The quote's own
    title is the fallback — it started as the task's and is usually still it.
    """
    if not request.source_task_id:
        return None
    row = await session.get(ProposalIndexItem, request.source_task_id)
    if row is not None and (row.title or "").strip():
        return row.title.strip()
    return request.title


async def resolve_folder(
    session: AsyncSession, drive: QuoteDrive, request: QuoteRequest
) -> tuple[str, str]:
    """The quote's folder, relative to the library root, and how it was found.

    Kept on the request once found. Returns ``(folder, how)`` where ``how`` is
    the match kind, or ``kept`` when the request already knew its folder.
    """
    if request.drive_folder:
        return request.drive_folder, "kept"

    own = storage.quote_folder_name(request.reference, request.id)
    title = await _task_title(session, request)
    if request.source_task_id and title:
        match: FolderMatch = await drive.find_task_folder(title)
        folder = f"{match.name}/{own}"
        how = match.how
    else:
        folder = f"{drive.unlinked_root}/{own}"
        how = "unlinked"
    request.drive_folder = folder
    request.filing_error = None
    return folder, how


async def file_upload(
    session: AsyncSession,
    drive: QuoteDrive,
    request: QuoteRequest,
    *,
    kind: DocumentKind | str,
    file_name: str,
    content: bytes,
    content_type: str | None,
    user: User | None,
    notes: str | None = None,
    supplier_quote_id: uuid.UUID | None = None,
) -> QuoteDocument:
    """File one uploaded document and record it on the quote.

    Raises :class:`DriveError` when a library is configured and refuses: the
    caller turns that into the message the person sees. With no library
    configured (a development box, the tests) the row is recorded unfiled and
    says so, so the rest of the flow can be exercised.
    """
    now = datetime.now(UTC)
    document = QuoteDocument(
        # Stamped here rather than by the database: a server-generated stamp
        # is unloaded after the flush, and the response is built before the
        # commit — reading it then is a lazy load inside async code.
        created_at=now,
        updated_at=now,
        kind=DocumentKind(kind),
        file_name=file_name[:255],
        content_type=(content_type or None),
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        notes=(notes or "").strip() or None,
        supplier_quote_id=supplier_quote_id,
        uploaded_by=user,
    )
    if drive.enabled:
        # Everything the library can throw — a refusal, a timeout, a dropped
        # connection, a surprise in its answer — becomes one DriveError with
        # the reason in it, which the route turns into the message the person
        # reads. Anything else escaping here reaches the browser as a bare
        # 500 with no CORS headers, which it reports as "could not reach".
        try:
            folder, how = await resolve_folder(session, drive, request)
            filed = await drive.file_document(
                folder=folder,
                filename=storage.document_file_name(kind_label(kind), file_name),
                content=content,
                content_type=content_type,
            )
        except DriveError:
            raise
        except httpx.HTTPError as exc:
            raise DriveError(f"the document library could not be reached: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - see above; the reason travels
            logger.exception("quote %s: filing %s failed unexpectedly", request.id, file_name)
            raise DriveError(f"filing failed: {type(exc).__name__}: {exc}") from exc
        document.drive_item_id = filed.item_id
        document.drive_url = filed.web_url
        document.drive_path = filed.path
        await _remember_folder_url(drive, request, folder)
        logger.info("quote %s: filed %s (%s folder)", request.id, file_name, how)
    else:
        document.notes = _unfiled_note(document.notes)
    # Appended to the loaded collection rather than added to the session, so
    # the response that is built next sees it without another query.
    request.documents.append(document)
    return document


async def file_report(
    session: AsyncSession,
    drive: QuoteDrive,
    request: QuoteRequest,
    *,
    content: bytes,
    reference: str,
    user: User | None,
) -> QuoteDocument | None:
    """File the selling & costing report for the pass being submitted.

    Best effort — see the module docstring. A failure is written onto
    ``request.filing_error`` and the submission carries on.
    """
    if not drive.enabled:
        return None
    name = f"{reference} Selling & Costing Report pass {request.revision}.pdf"
    try:
        folder, _ = await resolve_folder(session, drive, request)
        filed = await drive.file_document(
            folder=folder, filename=name, content=content, content_type="application/pdf"
        )
    except (DriveError, httpx.HTTPError) as exc:
        logger.warning("quote %s: report not filed: %s", request.id, exc)
        request.filing_error = f"The report for pass {request.revision} was not filed: {exc}"[:500]
        return None

    # One row per pass: submitting the same pass twice (a rework that was
    # re-sent) replaces the file in the library and the row here.
    for existing in list(request.documents):
        if existing.kind == DocumentKind.COSTING_REPORT and existing.revision == request.revision:
            request.documents.remove(existing)
    now = datetime.now(UTC)
    document = QuoteDocument(
        created_at=now,
        updated_at=now,
        kind=DocumentKind.COSTING_REPORT,
        file_name=name,
        content_type="application/pdf",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        revision=request.revision,
        uploaded_by=user,
        drive_item_id=filed.item_id,
        drive_url=filed.web_url,
        drive_path=filed.path,
    )
    request.documents.append(document)
    request.filing_error = None
    await _remember_folder_url(drive, request, folder)
    return document


async def remove_document(
    drive: QuoteDrive, request: QuoteRequest, document: QuoteDocument
) -> None:
    """Take a document off the quote, and out of the folder.

    The file goes too: it was this app that put it there, and a folder full of
    files the quote no longer lists is how a colleague prices from the wrong
    supplier quotation. If the library refuses, the row is still removed and
    the failure noted — the quote is the record of what it uses.
    """
    if drive.enabled and document.drive_item_id:
        try:
            await drive.delete_item(document.drive_item_id)
        except (DriveError, httpx.HTTPError) as exc:
            logger.warning("quote %s: could not delete %s: %s", request.id, document.file_name, exc)
            request.filing_error = (
                f"{document.file_name} was removed from the quote but is still in the "
                f"folder: {exc}"
            )[:500]
    request.documents.remove(document)


async def _remember_folder_url(drive: QuoteDrive, request: QuoteRequest, folder: str) -> None:
    if request.drive_folder_url:
        return
    try:
        request.drive_folder_url = await drive.folder_url(f"{drive.root}/{folder}")
    except (DriveError, httpx.HTTPError):
        # A link is a convenience; the file is already there.
        request.drive_folder_url = None


def _unfiled_note(notes: str | None) -> str:
    said = "Not filed: no document library is configured."
    return f"{notes}\n{said}" if notes else said


def summary(document: QuoteDocument) -> dict[str, Any]:
    """The row as the screen wants it, with the labels filled in."""
    return {
        "id": document.id,
        "kind": str(document.kind),
        "kind_label": kind_label(document.kind),
        "file_name": document.file_name,
        "content_type": document.content_type,
        "size": document.size,
        "drive_url": document.drive_url,
        "drive_path": document.drive_path,
        "uploaded_by_name": document.uploaded_by.display_name if document.uploaded_by else None,
        "created_at": document.created_at,
        "notes": document.notes,
        "supplier_quote_id": document.supplier_quote_id,
        "revision": document.revision,
        "suggestions": document.suggestions,
    }
