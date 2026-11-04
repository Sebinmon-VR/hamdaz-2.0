"""A folder for every new Proposals task, and its list attachments copied in.

What a Power Automate flow did until it was turned off on 2026-10-23 — make the
task's folder in the Proposal Team Channel, named after its title, as soon as
the task is created — and one thing it never did: put the files attached to the
task in the list into that folder, including ones attached days later.

Runs after each mirror sync (``app/intake/worker.py``), so a new task has its
folder within seconds of the list moving. Two steps, each bounded:

1. **Folders.** Tasks created since ``TASK_FOLDERS_SINCE`` that have none yet.
   A folder of the task's title is used if it is there, then one beginning
   with the same event number (the team renames folders), and only then is one
   made. Never a second one.
2. **Attachments.** Tasks with a folder whose list item has attachments and has
   changed since they were last read — attaching a file moves the item's
   Modified, which is how a new one is noticed. Each attachment not dealt with
   before goes straight into the task folder, addressed by the folder's id; a
   file of that name already there is left alone. A name once dealt with is
   never copied again, so a copy the team deletes stays deleted.

**What is written.** Folders and files in the Proposal Team Channel library,
nothing else. The Proposals list is read — its attachments included — and never
written to.
"""

from __future__ import annotations

import logging
import mimetypes
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.proposal_index import ProposalIndexItem
from app.models.task_folder import TaskFolder
from app.proposals.sharepoint import SharePointConsentError, SharePointError, SharePointProposals
from app.quoting.storage import (
    MAX_UPLOAD_BYTES,
    DriveConflictError,
    DriveError,
    DriveGoneError,
    QuoteDrive,
    safe_file_name,
)

logger = logging.getLogger("hamdaz.proposals.folders")

#: Beside the others (812_401 – 812_408), used by nothing else.
FOLDERS_LOCK: Final = 812_409

#: A task whose folder or files failed is tried again after this long, and its
#: folder at most this many times — a title the drive refuses is not retried
#: every few seconds for ever.
RETRY_AFTER: Final = timedelta(minutes=10)
MAX_TRIES: Final = 10

#: Bounds on one pass, so the backlog on the first day goes in a few passes
#: rather than one long one. Folders are a call or three each; attachments are
#: a download and an upload each, so they get a clock.
FOLDERS_PER_PASS: Final = 25
FILE_TASKS_PER_PASS: Final = 25
FILES_BUDGET_SECONDS: Final = 120

#: What the mirror calls a task with no title. Nothing to name a folder after.
UNTITLED: Final = "(untitled)"


@dataclass(slots=True)
class PassReport:
    folders_made: int = 0
    folders_found: int = 0
    folder_errors: int = 0
    files_copied: int = 0
    files_skipped: int = 0
    file_errors: int = 0

    @property
    def anything(self) -> bool:
        return any(
            (self.folders_made, self.folders_found, self.folder_errors,
             self.files_copied, self.files_skipped, self.file_errors)
        )


def since(settings: Settings) -> datetime:
    day = settings.task_folders_since
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def _take_lock(session: AsyncSession) -> bool:
    """One instance at a time — transaction-scoped, as the mirror's lock is."""
    return bool(
        await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": FOLDERS_LOCK})
    )


# ── 1. folders ─────────────────────────────────────────────────────────


async def make_folders(
    session: AsyncSession,
    drive: QuoteDrive,
    settings: Settings,
    report: PassReport,
    *,
    now: datetime | None = None,
) -> None:
    """Give each new task without one its folder."""
    now = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(ProposalIndexItem.item_id, ProposalIndexItem.title, TaskFolder)
            .outerjoin(TaskFolder, TaskFolder.item_id == ProposalIndexItem.item_id)
            .where(
                ProposalIndexItem.deleted.is_(False),
                ProposalIndexItem.sp_created_at >= since(settings),
                func.btrim(ProposalIndexItem.title).notin_(["", UNTITLED]),
                TaskFolder.done_at.is_(None),
                or_(
                    TaskFolder.item_id.is_(None),
                    and_(
                        TaskFolder.tries < MAX_TRIES,
                        or_(
                            TaskFolder.tried_at.is_(None),
                            TaskFolder.tried_at < now - RETRY_AFTER,
                        ),
                    ),
                ),
            )
            .order_by(ProposalIndexItem.sp_created_at.asc())
            .limit(FOLDERS_PER_PASS)
        )
    ).all()

    for item_id, title, row in rows:
        if row is None:
            row = TaskFolder(item_id=item_id, title=title.strip(), tries=0, files={})
            session.add(row)
        row.title = title.strip()
        row.tried_at = now
        try:
            match = await drive.find_task_folder(title)
            if match.how == "created":
                item: dict[str, Any] | None = await drive.create_folder(match.name)
                how = "made"
            else:
                item = {"id": match.item_id, "name": match.name, "webUrl": match.web_url}
                how = match.how
            if not item or not item.get("id"):
                # Made or found, but the drive did not say its id — ask once more.
                item = await drive.folder_item(match.name)
            if not item or not item.get("id"):
                raise DriveError("the drive did not return the folder")
        except (DriveError, httpx.HTTPError) as exc:
            row.tries = (row.tries or 0) + 1
            row.last_error = str(exc)[:500] or type(exc).__name__
            report.folder_errors += 1
            logger.warning("task %s: no folder yet (%s)", item_id, row.last_error)
            continue

        row.folder = str(item.get("name") or match.name)
        row.folder_id = str(item["id"])
        row.web_url = item.get("webUrl")
        row.how = how
        row.done_at = now
        row.last_error = None
        if how == "made":
            report.folders_made += 1
            logger.info("task %s: made folder %r", item_id, row.folder)
        else:
            report.folders_found += 1
    await session.flush()


# ── 2. attachments ─────────────────────────────────────────────────────


async def copy_attachments(
    session: AsyncSession,
    drive: QuoteDrive,
    sharepoint: SharePointProposals,
    report: PassReport,
    *,
    now: datetime | None = None,
    budget_seconds: float = FILES_BUDGET_SECONDS,
) -> None:
    """Copy into each task's folder the list attachments it does not have yet."""
    now = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(TaskFolder, ProposalIndexItem.sp_modified_at)
            .join(ProposalIndexItem, ProposalIndexItem.item_id == TaskFolder.item_id)
            .where(
                TaskFolder.done_at.is_not(None),
                TaskFolder.folder_id.is_not(None),
                ProposalIndexItem.deleted.is_(False),
                ProposalIndexItem.has_attachments.is_(True),
                or_(
                    TaskFolder.files_seen_at.is_(None),
                    ProposalIndexItem.sp_modified_at > TaskFolder.files_seen_at,
                ),
                or_(
                    TaskFolder.files_tried_at.is_(None),
                    TaskFolder.files_tried_at < now - RETRY_AFTER,
                ),
            )
            # Whatever moved last first, so a file attached a minute ago does
            # not wait behind the first day's backlog.
            .order_by(ProposalIndexItem.sp_modified_at.desc().nulls_last())
            .limit(FILE_TASKS_PER_PASS)
        )
    ).all()

    started = time.monotonic()
    for row, modified in rows:
        if time.monotonic() - started > budget_seconds:
            break
        row.files_tried_at = now
        try:
            errors = await _copy_task(drive, sharepoint, row, report, now=now)
        except SharePointConsentError as exc:
            # The same answer for every task: say so once and stop.
            row.files_error = str(exc)[:500]
            logger.warning("task folders: list attachments cannot be read: %s", exc)
            break
        except DriveGoneError as exc:
            # Deleted by the team, most likely. Not made again; looked at once
            # more only if the task changes.
            row.files_error = str(exc)[:500]
            row.files_seen_at = modified or now
            report.file_errors += 1
            continue
        except (SharePointError, DriveError, httpx.HTTPError) as exc:
            row.files_error = str(exc)[:500] or type(exc).__name__
            report.file_errors += 1
            logger.warning("task %s: attachments not copied (%s)", row.item_id, row.files_error)
            continue
        if errors:
            # What went in stays recorded; the rest is tried again later.
            row.files_error = "; ".join(errors)[:500]
            continue
        row.files_error = None
        row.files_seen_at = modified or now
    await session.flush()


async def _copy_task(
    drive: QuoteDrive,
    sharepoint: SharePointProposals,
    row: TaskFolder,
    report: PassReport,
    *,
    now: datetime,
) -> list[str]:
    """One task's new attachments into its folder; the failures, by name."""
    attachments = await sharepoint.attachments_of(row.item_id)
    files: dict[str, Any] = dict(row.files or {})
    pending = [a for a in attachments if a["file_name"].casefold() not in files]
    if not pending:
        return []
    present = await drive.names_in(str(row.folder_id))
    errors: list[str] = []
    for attachment in pending:
        name = attachment["file_name"]
        target = safe_file_name(name)
        entry: dict[str, Any] = {
            "name": name, "as": target, "at": now.isoformat(timespec="seconds")
        }
        if target.casefold() in present:
            entry["how"] = "already there"
            report.files_skipped += 1
        else:
            try:
                content = await sharepoint.attachment_content(row.item_id, name)
                entry["size"] = len(content)
                if len(content) > MAX_UPLOAD_BYTES:
                    entry["how"] = "too large"
                    report.files_skipped += 1
                    logger.warning("task %s: %s is too large to copy", row.item_id, name)
                else:
                    filed = await drive.file_into(
                        str(row.folder_id), name, content, mimetypes.guess_type(name)[0]
                    )
                    entry["how"] = "copied"
                    entry["url"] = filed.web_url
                    report.files_copied += 1
            except DriveConflictError:
                entry["how"] = "already there"
                report.files_skipped += 1
            except DriveGoneError:
                raise
            except (SharePointError, DriveError, httpx.HTTPError) as exc:
                errors.append(f"{name}: {exc}")
                report.file_errors += 1
                continue
        files[name.casefold()] = entry
        # A new dict each time, so the change is seen and what went in is kept
        # even if a later file fails.
        row.files = dict(files)
    return errors


# ── one pass ───────────────────────────────────────────────────────────


async def run_pass(
    factory: async_sessionmaker[AsyncSession],
    drive: QuoteDrive,
    sharepoint: SharePointProposals,
    settings: Settings,
) -> PassReport | None:
    """Folders, then attachments, each in its own transaction under the lock.
    None when another instance holds it."""
    report = PassReport()
    async with factory() as session:
        if not await _take_lock(session):
            return None
        await make_folders(session, drive, settings, report)
        await session.commit()
    if not settings.task_files_enabled:
        return report
    async with factory() as session:
        if not await _take_lock(session):
            return report
        await copy_attachments(session, drive, sharepoint, report)
        await session.commit()
    return report
