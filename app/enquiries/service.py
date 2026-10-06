"""Running an enquiry analysis, start to finish.

A run is minutes of work — downloads, one long model read, a history sweep, a
web lookup per few new items — so it runs in the background and says what it
is doing as it goes (``stage``), committed at each step so the page can show
it. The steps:

1. **Gather.** The task's list attachments, and every file in its folder in
   the Proposal Team Channel library (uploads from the page land there too).
   This app's own reports in the folder are not read back.
2. **Sort and read.** Files named like a supplier's quotation go to the
   comparison module's reader, which needs no model; if it finds priced lines
   the quotation is stored as a supplier quote, so the supplier library can be
   built from it. Everything else is read by Claude in one call into
   requirement lines. A file the model says is a quotation goes back to the
   quote reader.
3. **Match.** Each line against our supplier quotes, our quote requests and
   Zoho Books. No model.
4. **Look up.** New items on the web, when the run asks for it.
5. **Report.** A PDF and a workbook, filed into the task folder.

Nothing is written to the Proposals list, and nothing to Zoho.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.assistant.llm import LLMError
from app.comparison import service as comparison_service
from app.comparison.documents import DocumentError, Readable, prepare
from app.comparison.extraction import ExtractionError
from app.core.config import Settings
from app.enquiries import claude, matching, reading, report, research
from app.models.comparison import ComparisonStatus, QuoteComparison, SupplierQuote
from app.models.enquiry import EnquiryAnalysis, EnquiryDocument, EnquiryLine
from app.models.proposal_index import ProposalIndexItem
from app.models.user import User
from app.proposals.sharepoint import SharePointConsentError, SharePointError
from app.quoting.storage import DriveError, safe_name
from app.zoho.client import ZohoError

logger = logging.getLogger("hamdaz.enquiries")

#: Documents read per run. A task folder with more is an archive, not a bid.
MAX_DOCUMENTS = 25
#: Log entries kept per run; a run that writes more keeps the latest.
MAX_LOG = 400
#: A run that has said nothing for this long died with the server.
STALE_AFTER_SECONDS = 30 * 60

#: Runs in this process, so one cannot be started twice and a restart shows.
RUNNING: dict[uuid.UUID, asyncio.Task] = {}


class EnquiryError(Exception):
    """Something a person can act on. The message is written for them."""


@dataclass(slots=True)
class Deps:
    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    sharepoint: Any
    drive: Any
    extractor: Any
    llm: Any
    zoho: Any

    @property
    def model(self) -> str:
        return self.llm.model


# ── the task ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class TaskFacts:
    title: str
    end_user: str | None
    bid_closing_date: date | None


async def task_facts(session: AsyncSession, sharepoint: Any, task_id: str) -> TaskFacts:
    """The task's title and dates: from the local mirror, else from SharePoint."""
    row = await session.get(ProposalIndexItem, task_id)
    if row is not None and not row.deleted:
        return TaskFacts(row.title, row.end_user, row.bid_closing_date or row.due_date)
    try:
        task = await sharepoint.task(task_id)
    except SharePointError as exc:
        raise EnquiryError(f"No task {task_id} in the Proposals list ({exc}).") from exc
    return TaskFacts(task.title, task.end_user, task.closing_date)


async def for_task(session: AsyncSession, task_id: str) -> EnquiryAnalysis | None:
    return await session.scalar(select(EnquiryAnalysis).where(EnquiryAnalysis.task_id == task_id))


async def get_or_create(
    session: AsyncSession, sharepoint: Any, task_id: str, user: User
) -> EnquiryAnalysis:
    analysis = await for_task(session, task_id)
    facts = await task_facts(session, sharepoint, task_id)
    if analysis is None:
        analysis = EnquiryAnalysis(
            task_id=task_id,
            task_title=facts.title,
            end_user=facts.end_user,
            bid_closing_date=facts.bid_closing_date,
            status="idle",
            created_by_id=user.id,
            documents=[],
            lines=[],
        )
        session.add(analysis)
        await session.flush()
        await session.refresh(analysis)
    else:
        # The team renames tasks and moves dates; the analysis follows.
        analysis.task_title = facts.title
        analysis.end_user = facts.end_user
        analysis.bid_closing_date = facts.bid_closing_date
    return analysis


def settle_orphan(analysis: EnquiryAnalysis) -> bool:
    """Mark a run that this process is not running as interrupted. True if it did."""
    if analysis.status != "running" or analysis.id in RUNNING:
        return False
    analysis.status = "failed"
    analysis.error = "The run was interrupted (the server restarted). Run it again."
    analysis.finished_at = datetime.now(UTC)
    return True


# ── uploads ────────────────────────────────────────────────────────────


async def upload(
    session: AsyncSession,
    deps: Deps,
    analysis: EnquiryAnalysis,
    files: list[tuple[str, bytes, str | None]],
    user: User,
) -> list[EnquiryDocument]:
    """File each upload into the task's folder and record it.

    The library is the store, as it is for quote requests: a file that cannot
    be filed is refused, because there is nowhere else for it to be.
    """
    if not deps.drive.enabled:
        raise EnquiryError("No document library is configured, so there is nowhere to put the file.")
    try:
        folder = await deps.drive.find_task_folder(analysis.task_title)
    except DriveError as exc:
        raise EnquiryError(f"Could not find the task's folder: {exc}") from exc
    analysis.drive_folder = folder.name
    if folder.web_url:
        analysis.drive_folder_url = folder.web_url

    known = {d.origin_key: d for d in analysis.documents}
    added: list[EnquiryDocument] = []
    for name, content, content_type in files:
        try:
            filed = await deps.drive.file_document(
                folder=folder.name, filename=name, content=content, content_type=content_type
            )
        except DriveError as exc:
            raise EnquiryError(f"{name} could not be saved to the task folder: {exc}") from exc
        key = f"drive:{filed.item_id}"
        document = known.get(key)
        if document is None:
            document = EnquiryDocument(
                source="upload",
                origin_key=key,
                file_name=safe_name(name)[:255],
                path=safe_name(name),
                drive_item_id=filed.item_id,
                uploaded_by_id=user.id,
            )
            analysis.documents.append(document)
        document.size = len(content)
        document.web_url = filed.web_url
        document.status = "pending"
        document.note = None
        added.append(document)
    if analysis.drive_folder_url is None:
        analysis.drive_folder_url = await deps.drive.folder_url(f"{deps.drive.root}/{folder.name}")
    await session.flush()
    return added


# ── starting ───────────────────────────────────────────────────────────


def start(deps: Deps, analysis_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Run it in the background. The caller has already set ``status``."""
    task = asyncio.create_task(run(deps, analysis_id, user_id), name=f"enquiry-{analysis_id}")
    RUNNING[analysis_id] = task
    task.add_done_callback(lambda _t: RUNNING.pop(analysis_id, None))


async def run(deps: Deps, analysis_id: uuid.UUID, user_id: uuid.UUID) -> None:
    current: Run | None = None
    try:
        async with deps.factory() as session:
            analysis = await session.get(EnquiryAnalysis, analysis_id)
            user = await session.get(User, user_id)
            if analysis is None or user is None:
                return
            current = Run(session, deps, analysis, user)
            await current.go()
    except Exception as exc:  # noqa: BLE001 — a run must end in a state, whatever happened
        logger.exception("enquiry analysis %s failed", analysis_id)
        message = str(exc) if isinstance(exc, (EnquiryError, LLMError)) else (
            f"The analysis stopped on an error: {type(exc).__name__}: {exc}"
        )
        # The log so far, and why it stopped: the session that held it may
        # be the thing that failed, so it is written from a fresh one.
        log = list(current.log) if current is not None else []
        log.append(entry(f"Stopped: {message}", "error"))
        async with deps.factory() as session:
            await session.execute(
                update(EnquiryAnalysis)
                .where(EnquiryAnalysis.id == analysis_id)
                .values(
                    status="failed", error=message[:1000], finished_at=datetime.now(UTC),
                    run_log=log[-MAX_LOG:],
                )
            )
            await session.commit()


def entry(message: str, level: str = "info") -> dict[str, str]:
    """One log line: when, how it went (step, info, ok, warn, error), and what."""
    return {"at": datetime.now(UTC).isoformat(timespec="seconds"), "level": level, "message": message[:600]}


# ── the run ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Loaded:
    document: EnquiryDocument
    readable: Readable
    origin: str


class Run:
    def __init__(self, session: AsyncSession, deps: Deps, analysis: EnquiryAnalysis, user: User) -> None:
        self.session = session
        self.deps = deps
        self.analysis = analysis
        self.user = user
        self.notes: list[str] = []
        self.log: list[dict[str, str]] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.began = time.monotonic()
        # The web lookups run side by side and each writes to the log; the
        # session takes one commit at a time.
        self._commit = asyncio.Lock()

    async def say(self, message: str, level: str = "info", *, now: bool = True) -> None:
        """Add a line to the run's log and, unless ``now`` is off, show it to the page.

        Each showing is a commit against a database a third of a second away,
        so a line per item waits for the summary line after it.
        """
        self.log.append(entry(message, level))
        del self.log[:-MAX_LOG]
        self.analysis.run_log = list(self.log)
        if now:
            async with self._commit:
                await self.session.commit()

    async def warn(self, message: str) -> None:
        """Something the person should know about the result: a note and a log line."""
        self.notes.append(message)
        await self.say(message, "warn")

    async def stage(self, text: str) -> None:
        self.analysis.stage = text
        await self.say(text, "step")

    async def go(self) -> None:
        a = self.analysis
        a.status, a.error, a.started_at, a.finished_at = "running", None, datetime.now(UTC), None
        a.run_by_id = self.user.id
        a.model = self.deps.model
        a.run_log = []
        web = "on" if a.web_search else "off"
        await self.say(f"Started by {self.user.display_name} · model {a.model} · web lookup {web}")
        await self.stage("Finding the task's documents")

        files = await self.gather()
        await self.stage(f"Reading {len(files)} document(s)")
        loaded = await self.load(files)

        requirements, model_reading = await self.read(loaded)

        await self.stage(f"Checking {len(requirements)} item(s) against our history and Zoho")
        lines = await self.match(requirements)

        # The run's own choice; the setting is only its default (see the router).
        if a.web_search:
            fresh = [line for line in lines if line.status == "new"]
            if fresh:
                count = min(len(fresh), self.deps.settings.enquiry_web_max_lines)
                await self.stage(f"Looking up {count} new item(s) on the web")
                await self.look_up(fresh)

        a.lines.clear()
        await self.session.flush()
        for position, line in enumerate(lines):
            line.position = position
            a.lines.append(line)
        if model_reading is not None:
            a.summary = model_reading.summary or None
            a.customer = model_reading.customer or None
            a.deadline = model_reading.deadline or None
            a.conditions = model_reading.conditions
            a.missing = model_reading.missing
        else:
            # Nothing to read this time; an earlier run's summary would now be wrong.
            a.summary, a.customer, a.deadline, a.conditions, a.missing = None, None, None, [], []
            await self.warn(
                "No requirement documents were found: attach the RFQ or tender to the task, "
                "or add it on the Documents tab, and run again."
            )
        a.input_tokens, a.output_tokens = self.input_tokens, self.output_tokens
        a.cost_usd = self.cost()
        a.run_notes = self.notes
        await self.session.flush()

        await self.stage("Writing the reports")
        await self.file_reports()

        a.status, a.stage, a.finished_at = "done", None, datetime.now(UTC)
        await self.say(
            f"Done in {time.monotonic() - self.began:.0f}s · {len(lines)} item(s) · "
            f"{self.input_tokens:,} tokens in, {self.output_tokens:,} out · ${a.cost_usd} "
            "(web search fees not included)",
            "ok",
        )

    # ── 1. gather ──────────────────────────────────────────────────────

    async def gather(self) -> list[EnquiryDocument]:
        a, deps = self.analysis, self.deps
        seen: dict[str, dict[str, Any]] = {}

        try:
            for att in await deps.sharepoint.attachments_of(a.task_id):
                seen[f"attachment:{att['file_name']}"] = {
                    "source": "attachment", "file_name": att["file_name"], "path": None,
                    "size": None, "drive_item_id": None, "web_url": None,
                }
        except SharePointConsentError:
            # The app registration lacks the SharePoint Online permission the
            # attachments API needs; the task folder is still read.
            await self.warn(
                "The task's list attachments were not read: the app needs the 'Sites.Read.All' "
                "permission on the Office 365 SharePoint Online API (admin consent). "
                "The task folder was read instead."
            )
        except SharePointError as exc:
            await self.warn(f"The task's attachments could not be read: {exc}")
        else:
            await self.say(f"List attachments: {len(seen)} file(s)")

        if deps.drive.enabled:
            try:
                folder = await deps.drive.find_task_folder(a.task_title)
                a.drive_folder = folder.name
                a.drive_folder_url = folder.web_url or a.drive_folder_url
                if folder.how == "created":
                    await self.say(f"No folder for this task in Proposal Team Channel yet (it would be '{folder.name}')")
                else:
                    files = await deps.drive.list_files(folder.name)
                    await self.say(f"Task folder '{folder.name}': {len(files)} file(s)")
                    for f in files:
                        if reading.is_own_output(f["name"], f["path"], deps.settings.enquiry_report_folder):
                            await self.say(f"Left out {f['path']}: a report this app wrote")
                            continue
                        seen[f"drive:{f['id']}"] = {
                            "source": "folder", "file_name": f["name"], "path": f["path"],
                            "size": f["size"], "drive_item_id": f["id"], "web_url": f["web_url"],
                        }
            except DriveError as exc:
                await self.warn(f"The task folder could not be read: {exc}")

        by_key = {d.origin_key: d for d in a.documents}
        for key, document in list(by_key.items()):
            if key not in seen:
                a.documents.remove(document)  # gone from the task since the last run
        for key, info in seen.items():
            document = by_key.get(key)
            if document is None:
                document = EnquiryDocument(origin_key=key, source=info["source"], file_name=info["file_name"][:255])
                a.documents.append(document)
            document.path = info["path"]
            document.size = info["size"]
            document.drive_item_id = info["drive_item_id"]
            document.web_url = info["web_url"] or document.web_url
            document.status, document.note = "pending", None
        await self.session.flush()
        return list(a.documents)

    # ── 2. load ────────────────────────────────────────────────────────

    async def load(self, documents: list[EnquiryDocument]) -> list[Loaded]:
        deps = self.deps
        drive_ids = [d.drive_item_id for d in documents if d.drive_item_id]
        filed_quotes: set[str] = set()
        if drive_ids:
            filed_quotes = set(
                (await self.session.scalars(
                    select(SupplierQuote.drive_item_id).where(SupplierQuote.drive_item_id.in_(drive_ids))
                )).all()
            )

        loaded: list[Loaded] = []
        for document in documents:
            name = document.path or document.file_name
            if document.drive_item_id in filed_quotes and document.supplier_quote_id is None:
                document.kind, document.status = "supplier_quote", "skipped"
                document.note = "Already read on a quote request."
                await self.say(f"{name}: already read on a quote request, skipped")
                continue
            if len(loaded) >= MAX_DOCUMENTS:
                document.status, document.note = "skipped", f"Only {MAX_DOCUMENTS} documents are read per run."
                await self.say(f"{name}: skipped, only {MAX_DOCUMENTS} documents are read per run", "warn")
                continue
            try:
                if document.source == "attachment":
                    content = await deps.sharepoint.attachment_content(self.analysis.task_id, document.file_name)
                    origin = "task attachment"
                else:
                    content = await deps.drive.download(document.drive_item_id)
                    origin = f"task folder: {document.path or document.file_name}"
                readable = await asyncio.to_thread(prepare, document.file_name, content, None, ocr=False)
            except DocumentError as exc:
                document.status, document.note = "skipped", str(exc)
                await self.say(f"{name}: skipped, {exc}", "warn")
                continue
            except (SharePointError, SharePointConsentError, DriveError) as exc:
                document.status, document.note = "failed", f"Could not download it: {exc}"
                await self.say(f"{name}: could not download it, {exc}", "error")
                continue
            document.size = document.size or len(content)
            loaded.append(Loaded(document, readable, origin))
            if readable.kind == "text":
                shape = f"text, {len(readable.text or ''):,} characters"
            elif readable.kind == "image":
                shape = "an image, sent to Claude to look at"
            else:
                shape = "a scan, sent to Claude page by page"
            await self.say(f"Opened {name} ({len(content) // 1024:,} KB): {shape}")
        await self.session.flush()
        return loaded

    # ── 3. read ────────────────────────────────────────────────────────

    async def read(self, loaded: list[Loaded]) -> tuple[list[reading.Requirement], reading.Reading | None]:
        for_model: list[Loaded] = []
        for item in loaded:
            d = item.document
            if d.supplier_quote_id is not None:
                d.kind, d.status, d.note = "supplier_quote", "read", "Stored as a supplier quote on an earlier run."
                await self.say(f"{d.file_name}: supplier quote, stored on an earlier run")
                continue
            if (
                reading.guess_kind(d.file_name) == "supplier_quote"
                and item.readable.kind == "text"
                and await self.store_quote(item)
            ):
                continue
            for_model.append(item)

        if not for_model:
            await self.session.flush()
            return [], None

        if not self.deps.llm.configured:
            raise EnquiryError("Reading the requirement documents needs an Anthropic key (ANTHROPIC_API_KEY).")

        a = self.analysis
        context = (
            f"Enquiry: {a.task_title}\n"
            f"End user: {a.end_user or 'not given'}\n"
            f"Bid closing date: {a.bid_closing_date.isoformat() if a.bid_closing_date else 'not given'}"
        )
        prepared = reading.parts_for([(i.readable, i.origin) for i in for_model], context=context)
        for left in prepared.left_out:
            await self.warn(left)
        await self.stage(f"Reading the requirements in {len(prepared.names)} document(s)")
        await self.say(f"Asked Claude to read: {', '.join(prepared.names)}")
        asked = time.monotonic()
        raw, tokens_in, tokens_out = await self.deps.llm.read_documents(
            instructions=reading.INSTRUCTIONS,
            content=prepared.parts,
            schema=reading.SCHEMA,
            user_key=str(self.user.id),
        )
        self.input_tokens += tokens_in
        self.output_tokens += tokens_out
        try:
            result = reading.parse(raw)
        except ValueError as exc:
            raise EnquiryError(str(exc)) from exc
        await self.say(
            f"Claude answered in {time.monotonic() - asked:.0f}s: {len(result.items)} item(s), "
            f"{len(result.conditions)} condition(s), {len(result.missing)} open question(s) · "
            f"{tokens_in:,} tokens in, {tokens_out:,} out · ${claude.cost_of(self.deps.model, tokens_in, tokens_out)}",
            "ok",
        )
        for name, kind in result.kinds.items():
            await self.say(f"{name}: read as {kind.replace('_', ' ')}")

        now = datetime.now(UTC)
        for item in for_model:
            d = item.document
            if d.file_name not in prepared.names:
                d.status, d.note = "skipped", "Too large to send with the rest."
                continue
            kind = result.kinds.get(d.file_name, "requirement")
            if kind == "supplier_quote":
                if item.readable.kind == "text" and await self.store_quote(item):
                    continue
                d.note = "Looks like a supplier quotation, but its prices could not be read. Type it in on a quote request."
            d.kind, d.status, d.read_at = kind, "read", now
        await self.session.flush()
        return result.items, result

    async def store_quote(self, item: Loaded) -> bool:
        """Read a quotation with the comparison reader and keep it. True if stored."""
        d = item.document
        await self.say(f"{d.file_name}: looks like a supplier quotation, reading its prices")
        try:
            extracted = await self.deps.extractor.read(item.readable)
        except ExtractionError as exc:
            await self.say(f"{d.file_name}: no priced table found ({exc})")
            return False
        if not any(i.unit_price and i.unit_price > 0 for i in extracted.items):
            await self.say(f"{d.file_name}: no prices found, read as a requirement instead")
            return False
        incoming = comparison_service.quote_in_from(extracted, d.file_name)
        row = comparison_service.build_row(incoming)
        row.drive_item_id = d.drive_item_id
        row.drive_url = d.web_url
        comparison = await self.comparison()
        comparison.quotes.append(row)
        await self.session.flush()
        d.kind, d.status, d.read_at = "supplier_quote", "read", datetime.now(UTC)
        d.supplier_quote_id = row.id
        d.note = f"Stored as a supplier quote from {row.supplier_name}, {len(row.items)} line(s)."
        await self.say(f"{d.file_name}: {d.note}", "ok")
        return True

    async def comparison(self) -> QuoteComparison:
        """The comparison that holds this enquiry's supplier quotes, made on first need."""
        a = self.analysis
        if a.comparison_id is not None:
            found = await self.session.get(QuoteComparison, a.comparison_id)
            if found is not None:
                await self.session.refresh(found, attribute_names=["quotes"])
                return found
        comparison = QuoteComparison(
            title=f"Enquiry: {a.task_title}"[:200],
            reference=a.task_id[:100],
            notes="Supplier quotes found among the task's documents by the enquiry analysis.",
            currency="AED",
            status=ComparisonStatus.SAVED,
            created_by=self.user,
            quotes=[],
        )
        self.session.add(comparison)
        await self.session.flush()
        a.comparison_id = comparison.id
        return comparison

    # ── 4. match ───────────────────────────────────────────────────────

    async def match(self, requirements: list[reading.Requirement]) -> list[EnquiryLine]:
        if not requirements:
            return []
        deps, a = self.deps, self.analysis
        rows = await matching.load_pool(self.session, task_id=a.task_id, comparison_id=a.comparison_id)
        quoted = sum(1 for r in rows if r.source == "supplier_quote")
        await self.say(f"History: {quoted:,} supplier-quote line(s), {len(rows) - quoted:,} line(s) of our own quotes")
        if deps.zoho is not None and deps.zoho.configured:
            try:
                items = await matching.zoho_items(deps.zoho)
                rows.extend(items)
                await self.say(f"Zoho items catalogue: {len(items):,} item(s)")
            except ZohoError as exc:
                await self.warn(f"Zoho's items could not be read: {exc}")
        else:
            await self.say("Zoho is not connected; its history was not checked", "warn")
        pool = matching.Pool(rows)

        lines: list[EnquiryLine] = []
        zoho_budget = matching.ZOHO_LINES
        zoho_ok = deps.zoho is not None and deps.zoho.configured
        for req in requirements:
            found = pool.match(req.description, req.part_number, req.brand)
            current = [(v, r) for v, r in found if r.current]
            past = [(v, r) for v, r in found if not r.current][: matching.KEEP]
            history = [matching.history_entry(v, r) for v, r in past]

            best_item = next(((v, r) for v, r in past if r.source == "zoho_item" and v >= 0.8), None)
            if best_item is not None and zoho_ok and zoho_budget > 0:
                zoho_budget -= 1
                try:
                    for doc in await matching.zoho_documents(deps.zoho, best_item[1].zoho_item_id or ""):
                        history.append(
                            {**doc, "description": best_item[1].ref, "part_number": best_item[1].part_number,
                             "supplier": None, "cost_rate": None, "quantity": None, "score": best_item[0],
                             "zoho_item_id": best_item[1].zoho_item_id}
                        )
                except ZohoError as exc:
                    zoho_ok = False
                    await self.warn(f"Zoho's quotes could not be read: {exc}")

            lines.append(
                EnquiryLine(
                    description=req.description,
                    part_number=req.part_number,
                    brand=req.brand,
                    quantity=req.quantity,
                    unit=req.unit,
                    specification=req.specification,
                    source_document=req.source_document,
                    status=matching.status_of(history, recent_months=deps.settings.enquiry_recent_months),
                    history=history,
                    suppliers=matching.suppliers_from(history, current),
                    web=None,
                )
            )
            line = lines[-1]
            where = ""
            if line.history:
                first = line.history[0]
                where = f", last in {first['source'].replace('_', ' ')} {first['ref']}"
            await self.say(f"#{len(lines)} {req.description[:70]}: {line.status}{where}", now=False)
        counts = {s: sum(1 for x in lines if x.status == s) for s in ("recent", "history", "new")}
        await self.say(
            f"Matched {len(lines)} item(s): {counts['recent']} seen recently, "
            f"{counts['history']} in our history, {counts['new']} new",
            "ok",
        )
        return lines

    # ── 5. look up ─────────────────────────────────────────────────────

    async def look_up(self, fresh: list[EnquiryLine]) -> None:
        settings = self.deps.settings
        chosen = fresh[: settings.enquiry_web_max_lines]
        if len(fresh) > len(chosen):
            await self.warn(
                f"{len(fresh) - len(chosen)} new item(s) were not looked up on the web "
                f"(at most {settings.enquiry_web_max_lines} per run)."
            )
        batches = [chosen[i : i + research.BATCH] for i in range(0, len(chosen), research.BATCH)]
        gate = asyncio.Semaphore(3)
        failures: list[str] = []

        async def one(number: int, batch: list[EnquiryLine]) -> None:
            asks = [
                research.Lookup(n + 1, line.description, line.part_number, line.brand, line.specification)
                for n, line in enumerate(batch)
            ]
            async with gate:
                await self.say(f"Web lookup {number}/{len(batches)}: {len(batch)} item(s), searching…")
                asked = time.monotonic()
                try:
                    raw, tokens_in, tokens_out = await self.deps.llm.research(
                        instructions=research.INSTRUCTIONS,
                        prompt=research.prompt_for(asks, self.analysis.end_user),
                        schema=research.SCHEMA,
                        user_key=str(self.user.id),
                    )
                except LLMError as exc:
                    failures.append(str(exc))
                    await self.say(f"Web lookup {number}/{len(batches)} failed: {exc}", "error")
                    return
            self.input_tokens += tokens_in
            self.output_tokens += tokens_out
            answers = research.parse(raw)
            makers = sum(1 for v in answers.values() if v.get("manufacturer"))
            sellers = sum(len(v.get("suppliers") or []) for v in answers.values())
            await self.say(
                f"Web lookup {number}/{len(batches)} done in {time.monotonic() - asked:.0f}s: "
                f"maker for {makers} of {len(batch)}, {sellers} supplier(s) · "
                f"${claude.cost_of(self.deps.model, tokens_in, tokens_out)}",
                "ok",
            )
            for n, line in enumerate(batch):
                found = answers.get(n + 1)
                if found is None:
                    continue
                suppliers = found.pop("suppliers")
                line.web = {**found, "looked_up_at": datetime.now(UTC).isoformat()}
                known = {(s.get("name") or "").casefold() for s in line.suppliers or []}
                line.suppliers = list(line.suppliers or []) + [
                    s for s in suppliers if s["name"].casefold() not in known
                ]

        await asyncio.gather(*(one(n, b) for n, b in enumerate(batches, start=1)))
        if failures:
            self.notes.append(f"The web lookup failed for some items: {failures[0]}")

    # ── 6. report ──────────────────────────────────────────────────────

    async def file_reports(self) -> None:
        a, deps = self.analysis, self.deps
        a.filing_error = None
        if not deps.drive.enabled or not a.drive_folder:
            await self.say("Reports not filed: no document library is configured", "warn")
            return
        stem = report.file_stem(a)
        folder = f"{a.drive_folder}/{deps.settings.enquiry_report_folder}"
        try:
            pdf = await asyncio.to_thread(report.pdf, a)
            book = await asyncio.to_thread(report.workbook, a)
            filed_pdf = await deps.drive.file_document(
                folder=folder, filename=f"{stem}.pdf", content=pdf, content_type="application/pdf"
            )
            filed_book = await deps.drive.file_document(
                folder=folder, filename=f"{stem}.xlsx", content=book,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            a.report_pdf_url, a.report_xlsx_url = filed_pdf.web_url, filed_book.web_url
            await self.say(f"Reports saved to {folder}: {stem}.pdf and .xlsx", "ok")
        except DriveError as exc:
            a.filing_error = f"The reports could not be saved to the task folder: {exc}"
            await self.say(a.filing_error, "warn")

    def cost(self) -> Decimal:
        return claude.cost_of(self.deps.model, self.input_tokens, self.output_tokens)
