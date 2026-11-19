"""A task's files, and whether a submitted task has its costing sheet.

The user, 2026-11-19: "if the users submitted and there is no costing sheet
... it should ask the user for it", and "if any user asks for any files or
attachments for the task it should also get the files to the users".

**A task has its costing** (the user's choice, "either one") when there is a
costing in the ERP linked to it (``quote_requests.source_task_id``) **or** a
file on the task whose name says costing: an attachment on its Proposals list
item, or a file in its task folder in the Proposal Team Channel ("Costing
Sheet.xlsx", "QR-0042 Selling & Costing Report pass 1.pdf"). Only when
neither is there is the holder asked, once per task (``sent``).

**A task's files** are the same two places, read only: its list item's
attachments (SharePoint REST) and its task folder (the quote drive). They go
to the person as links, which open in SharePoint with their own access.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import quote, urlsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.followups import service as fu
from app.itsupport import tasklist
from app.itsupport.status import Asked, Item
from app.proposals.sharepoint import ProposalTask

logger = logging.getLogger("hamdaz.itsupport")

#: A file name that says it is the costing.
COSTING_FILE: Final = re.compile(
    r"costing|cost[\s_-]*(?:sheet|working|report)|selling\s*&?\s*cost", re.I)
#: Submitted tasks looked at: BCD from this long ago ...
SINCE: Final = timedelta(days=3)
#: ... to this far ahead (submitted early).
UNTIL: Final = timedelta(days=14)


def key(task_id: str, *, test: bool = False) -> str:
    """The once-ever flag of the costing ask for a task. A test's own, so
    going live later still asks the holder."""
    return f"costing-{'test' if test else 'ask'}:{task_id}"


def test_to(settings: Any) -> str:
    """Whose chat every costing ask goes into while it's being tried; "" when
    it's live (each holder)."""
    return (getattr(settings, "it_support_try_to", "") or "").strip().lower()


def may_try(settings: Any, email: str, task: ProposalTask) -> bool:
    """While it's being tried, only the tester's own tasks with "test" in the
    title (the user, 2026-11-19: "test it for a test task"); live, any."""
    to = test_to(settings)
    return not to or (email.strip().lower() == to and "test" in (task.title or "").casefold())


def submitted(tasks: list[ProposalTask], now: datetime) -> list[ProposalTask]:
    """Their tasks marked Submitted with a BCD in the window."""
    out = []
    for task in tasks:
        if not fu.is_submitted(task.submission_status):
            continue
        due = fu.due_of(task)
        if due is not None and now - SINCE <= due <= now + UNTIL:
            out.append(task)
    return out


@dataclass(slots=True)
class File:
    name: str
    #: "task folder" or "on the list".
    where: str
    link: str
    path: str = ""
    #: Its drive item, for a file in the task folder: what makes a Teams file
    #: card possible (a list attachment isn't a drive item: a link only).
    item_id: str = ""


#: A Teams file card: (attachment id, the file's own address, its name).
Card = tuple[str, str, str]

_GUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                          re.I)


class Files:
    """Reads a task's files and its costing, read only."""

    def __init__(self, factory: async_sessionmaker[AsyncSession] | None, sharepoint: Any,
                 drive: Any) -> None:
        self._factory = factory
        self._sharepoint = sharepoint
        self._drive = drive

    def _site(self) -> str:
        url = getattr(getattr(self._sharepoint, "_settings", None),
                      "sharepoint_proposals_list_url", "") or ""
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""

    async def folder_of(self, task_id: str) -> tuple[str | None, str | None]:
        """The task folder's name under the drive's root, and its link."""
        from app.models.task_folder import TaskFolder

        if self._factory is None:
            return None, None
        async with self._factory() as session:
            row = await session.get(TaskFolder, task_id)
        return (row.folder, row.web_url) if row is not None else (None, None)

    async def of(self, task: ProposalTask | str, *, has_attachments: bool = True
                 ) -> tuple[list[File], str | None, list[str]]:
        """The task's files (folder first), the folder's link, and what
        couldn't be read (the reason, said once each)."""
        task_id = task if isinstance(task, str) else task.id
        if not isinstance(task, str):
            has_attachments = task.has_attachments
        files: list[File] = []
        problems: list[str] = []
        folder, folder_link = await self.folder_of(task_id)
        if folder and self._drive is not None and getattr(self._drive, "enabled", False):
            try:
                for f in await self._drive.list_files(folder):
                    files.append(File(f["name"], "task folder", f.get("web_url") or "",
                                      f.get("path") or f["name"], f.get("id") or ""))
            except Exception as exc:  # noqa: BLE001 - the list's files may still come
                logger.warning("it support: task folder of %s unreadable: %s", task_id, exc)
                problems.append("the task folder couldn't be read just now")
        if has_attachments and self._sharepoint is not None:
            try:
                site = self._site()
                for a in await self._sharepoint.attachments_of(task_id):
                    files.append(File(a["file_name"], "on the list",
                                      f"{site}{quote(a['server_url'])}" if site else ""))
            except Exception as exc:  # noqa: BLE001
                logger.warning("it support: attachments of %s unreadable: %s", task_id, exc)
                problems.append("the list's attachments couldn't be read just now")
        # The app copies a task's list attachments into its folder: one each.
        seen: set[str] = set()
        once = []
        for f in files:
            name = " ".join(f.name.replace("\xa0", " ").casefold().split())
            if name not in seen:
                seen.add(name)
                once.append(f)
        return once, folder_link, problems

    async def card(self, f: File) -> Card | None:
        """A file in the task folder as a Teams file card (the user,
        2026-11-19: "like the docs with preview card, not the link"). None
        for a file that can't be one (a list attachment); it goes as a link."""
        if not f.item_id or self._drive is None:
            return None
        try:
            item = await self._drive.item(f.item_id)
        except Exception as exc:  # noqa: BLE001 - the link still gets it there
            logger.warning("it support: no card for %s: %s", f.name, exc)
            return None
        guid = _GUID.search(item.get("eTag") or "")
        url = item.get("webDavUrl") or item.get("webUrl")
        if guid is None or not url:
            return None
        return guid.group(0).lower(), url, item.get("name") or f.name

    async def save(self, task: ProposalTask, name: str, data: bytes,
                   media_type: str | None = None) -> File:
        """A costing sheet someone sent in chat, put in the task's folder
        (found as the app finds it, made if there's none), named so it reads
        as the costing ("Costing Sheet - …" unless it says so already)."""
        from app.quoting.storage import DriveConflictError

        if self._drive is None or not getattr(self._drive, "enabled", False):
            raise RuntimeError("no drive for task folders here")
        folder_id = await self._folder_id(task)
        if not COSTING_FILE.search(name):
            name = f"Costing Sheet - {name}"
        stem, dot, ext = name.rpartition(".")
        for n in range(1, 6):
            try_name = name if n == 1 else (f"{stem} ({n}).{ext}" if dot else f"{name} ({n})")
            try:
                filed = await self._drive.file_into(folder_id, try_name, data, media_type)
            except DriveConflictError:
                continue
            return File(filed.path or try_name, "task folder", filed.web_url, "", filed.item_id)
        raise RuntimeError(f"{name} is in the folder five times already")

    async def _folder_id(self, task: ProposalTask) -> str:
        from app.models.task_folder import TaskFolder

        if self._factory is not None:
            async with self._factory() as session:
                row = await session.get(TaskFolder, task.id)
            if row is not None and row.folder_id:
                return row.folder_id
        match = await self._drive.find_task_folder(task.title)
        if match.item_id:
            return match.item_id
        made = await self._drive.create_folder(match.name)
        if not made or not made.get("id"):
            raise RuntimeError(f"the task folder {match.name!r} couldn't be made")
        return made["id"]

    async def without_costing(self, tasks: list[ProposalTask]) -> list[ProposalTask] | None:
        """The tasks with no costing anywhere. None: something couldn't be
        read, so it isn't sure (ask nothing; the next round tries again)."""
        from app.quoting.service import quotes_for_tasks

        if self._factory is None or not tasks:
            return [] if not tasks else None
        async with self._factory() as session:
            quoted = await quotes_for_tasks(session, [t.id for t in tasks])
        out = []
        for task in tasks:
            if task.id in quoted:
                continue
            files, _, problems = await self.of(task)
            if any(COSTING_FILE.search(f.name) for f in files):
                continue
            if problems:
                return None
            out.append(task)
        return out


# ── what's said ────────────────────────────────────────────────────────


def make_link(app: str, task_id: str) -> str:
    """The ERP's new-costing page opened on this exact task, so it's raised
    under the task (the user, 2026-11-19: "in the name of the task, not the
    new ... need to select that exact task")."""
    return f"{app}/quote-requests/new?task={task_id}"


def _a(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


#: What they can do, in the order the user gave (2026-11-19): a sheet made
#: outside the ERP is sent here or attached on the list; only with no sheet
#: at all is it made in the ERP, under the task.
_WAYS: Final = (
    "If you made {it} somewhere else, just send {it} to me here and I'll put {it} in the "
    "task's folder, or attach {it} to the task on the Proposals list.")


def ask(first_name: str, missing: list[ProposalTask], now: datetime, app: str
        ) -> tuple[str, str, Asked]:
    """The ask: its first line (for the log), the message, and its numbers."""
    items = [Item(n, t.id, t.title or "") for n, t in enumerate(missing, start=1)]
    one = len(missing) == 1
    ways = html.escape(_WAYS.format(it="it" if one else "them"))
    if one:
        task = missing[0]
        line = (f"Hi {first_name}, {task.id} · {tasklist.tender(task.title or '', 50)} shows "
                "submitted, but I can't find a costing sheet for it, not in the ERP and not "
                "on the task.")
        make = (f"No costing sheet at all? Then make it in the ERP under this task: "
                f"{_a(make_link(app, task.id), f'Make the costing for {task.id}')}.")
        return (line, f"<p>{html.escape(line)}</p><p>{ways}</p><p>{make}</p>",
                Asked("costing", items, now))
    line = (f"Hi {first_name}, these show submitted, but I can't find a costing sheet for "
            "them, not in the ERP and not on the task:")
    cells = []
    for n, task in enumerate(missing, start=1):
        due = fu.due_of(task)
        uae, ist = tasklist.when_cells(due, now) if due else ("", "")
        cells.append([str(n), task.id, tasklist.tender(task.title or "", tasklist.TENDER_CHARS),
                      uae, ist, tasklist.Html(_a(make_link(app, task.id), "Make in ERP"))])
    make = ("No costing sheet at all for one? Then make it in the ERP under that task: the "
            "link on its row opens it.")
    body = (f"<p>{html.escape(line)}</p>"
            + tasklist.grid(["#", "Task", "Tender", "BCD (UAE)", "IST", "No sheet?"], cells)
            + f"{tasklist.SPACE}<p>{ways}</p><p>{html.escape(make)}</p>")
    return line, body, Asked("costing", items, now)


def after_filing(missing: list[ProposalTask], app: str) -> str:
    """Said after "1958 submitted" when it has no costing anywhere."""
    ids = ", ".join(t.id for t in missing)
    one = len(missing) == 1
    links = ", ".join(f"[{t.id}]({make_link(app, t.id)})" for t in missing)
    return (f"I can't find a costing sheet for {ids}, not in the ERP and not on the task. "
            + _WAYS.format(it="it" if one else "them")
            + f" If there's none at all, make it in the ERP under the task: {links}.")


def for_model(task: ProposalTask | None, task_id: str, title: str, files: list[File],
              folder_link: str | None, problems: list[str]) -> dict[str, Any]:
    """A task's files for the brain's task_files tool."""
    return {
        "task": {"id": task_id, "title": title},
        "folder": folder_link,
        "files": [{"name": f.name, "where": f.where, "link": f.link,
                   "path": f.path if f.path != f.name else ""} for f in files[:40]],
        "more": max(0, len(files) - 40),
        "couldnt_read": problems,
        "how_to_give": "Files you sent are in the chat as cards already: don't paste their "
                       "links. Any other file they asked for: [name](link). Say if one asked "
                       "for isn't there.",
    }
