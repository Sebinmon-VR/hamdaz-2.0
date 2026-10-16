"""Running an agent: its steps, one after another, on one task.

**The system first, the model last.** The user's rule (2026-10-15): "maximum
use our system, then use AI". So every step below is worked out from what the
app already holds — the task mirror, the task's documents and the reader's
tables, our supplier quotes and quote requests, a finished enquiry analysis,
templates — and the model (Claude Haiku 5.5, see ``model.py``) is asked only
for what is left: a scan with no table to read, an instruction the owner
wrote in their own words, a web search they switched on, wording they asked
to have polished. Each step says which it used, and what it cost.

**Every result is visible, every fact has a source.** A step records what it
produced and where each part came from — a document, the task, one of our
quotes, a web page — and anything the material does not say is "not given",
never guessed. A run always ends with a summary on its page and a
notification to its owner, whatever happened.

**Nothing leaves the app without a yes.** The task's documents are read, not
written; the Proposals list and Zoho are not touched. Emails are drafts —
except through "Send the emails, once I say yes", which shows its owner every
email in full in the app to read and edit, and sends only the ones they tick
and then confirm, from their own mailbox. Teams can only point to the app or
say don't send.

**It talks in Teams.** A question (the task's documents, sending the RFQs)
goes to its owner as a card through their Power Automate flow, and the
answer comes back to ``/agents/teams/answer``; the results go there too. Not
by mail unless AGENT_EMAIL_NOTIFY is on.

A run lives in this process (``RUNNING``) while it works or waits; its row is
written after every step, so the page can follow it, and it beats
(``heartbeat_at``) while it waits. An answer is written to the run itself
(``question.answer``) as well as handed to the runner, so it reaches a run in
another process, or one a restart cut off: a run waiting for documents starts
again from the top, and one waiting to send sends from its saved drafts (see
``app.agents.worker`` and ``answer_cut_off``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.model import AgentModel, Spent
from app.comparison.documents import DocumentError, Readable, prepare
from app.core.config import Settings
from app.core.llm import TextModel
from app.core.mail import GraphMailer
from app.enquiries import matching, reading
from app.enquiries.claude import LLMError
from app.models.agent import AgentRun, UserAgent
from app.models.comparison import SupplierQuote
from app.models.enquiry import EnquiryAnalysis
from app.models.notification import NotificationKind
from app.models.proposal_index import ProposalIndexItem
from app.models.user import User
from app.notifications.service import notify, send_to_teams
from app.proposals.sharepoint import SharePointConsentError, SharePointError
from app.quoting.storage import DriveError

logger = logging.getLogger("hamdaz.agents")

#: Documents read per run, as for an enquiry analysis.
MAX_DOCUMENTS: Final = 25
#: How long a run waits for its owner to answer before it gives up.
ANSWER_TIMEOUT: Final = timedelta(hours=24)
#: A waiting run looks for an answer written by another process this often.
ANSWER_POLL_SECONDS: Final = 5
#: And says it is still alive this often.
HEARTBEAT_SECONDS: Final = 30
#: A live run whose heartbeat is older than this was cut off.
CUT_OFF_AFTER: Final = timedelta(minutes=3)
#: Questions a cut-off run can still be answered on: documents start it
#: again, sending sends from the saved drafts.
RESUMABLE: Final = frozenset({"docs", "send"})
_EMAIL: Final = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")
#: Characters of material handed to the model in one step.
MATERIAL_BUDGET: Final = 40_000
NOT_GIVEN: Final = "not given"

#: Runs in this process, by run id.
RUNNING: dict[uuid.UUID, Runner] = {}


# ── the catalogue ──────────────────────────────────────────────────────

#: Every block the builder offers: kind -> (name, group). The frontend's
#: ``lib/agents.ts`` holds the same list with its settings; a kind not here
#: cannot be saved.
KINDS: Final[dict[str, tuple[str, str]]] = {
    "start_manual": ("When I ask", "start"),
    "start_new_task": ("A task is assigned to me", "start"),
    "start_new_tender": ("A new Ariba tender arrives", "start"),
    "start_schedule": ("On a schedule", "start"),
    "start_email": ("An email arrives", "start"),
    "read_documents": ("Read the task's documents", "read"),
    "extract_requirements": ("Pull out items & requirements", "read"),
    "lookup_tasks": ("Look up my tasks", "read"),
    "lookup_history": ("Search past quotes", "read"),
    "read_supplier_quotes": ("Read supplier quotes", "read"),
    "ask_model": ("Think it through", "think"),
    "find_suppliers": ("Find suppliers", "think"),
    "compare_quotes": ("Compare supplier quotes", "think"),
    "summarise": ("Summarise", "think"),
    "check": ("Check a condition", "think"),
    "ask_documents": ("Ask for the documents", "ask"),
    "ask_me": ("Ask me", "ask"),
    "get_approval": ("Get approval", "ask"),
    "draft_email": ("Draft an email", "act"),
    "send_emails": ("Send the emails, once I say yes", "act"),
    "draft_quote_request": ("Draft a quote request", "act"),
    "make_document": ("Make a document", "act"),
    "notify_me": ("Tell me", "act"),
    "wait_replies": ("Wait for replies", "wait"),
    "wait_status": ("Wait until…", "wait"),
    "pause": ("Pause", "wait"),
}

MAX_STEPS: Final = 40


def validate_steps(raw: Any) -> list[dict[str, Any]]:
    """The builder's steps, checked: known kinds, a trigger only first, small configs."""
    if not isinstance(raw, list):
        raise ValueError("Steps must be a list.")
    if len(raw) > MAX_STEPS:
        raise ValueError(f"An agent can have at most {MAX_STEPS} steps.")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, step in enumerate(raw):
        if not isinstance(step, dict):
            raise ValueError(f"Step {index + 1} is not an object.")
        kind = str(step.get("kind") or "")
        if kind not in KINDS:
            raise ValueError(f"Step {index + 1}: there is no block called {kind!r}.")
        if KINDS[kind][1] == "start" and index != 0:
            raise ValueError("Only the first step can say what starts the agent.")
        step_id = str(step.get("id") or uuid.uuid4())[:64]
        if step_id in seen:
            step_id = str(uuid.uuid4())
        seen.add(step_id)
        config = step.get("config") or {}
        if not isinstance(config, dict):
            raise ValueError(f"Step {index + 1}: its settings must be an object.")
        clean: dict[str, Any] = {}
        for key, value in list(config.items())[:30]:
            if isinstance(value, (str, int, float, bool)) or value is None:
                clean[str(key)[:40]] = value[:4000] if isinstance(value, str) else value
        out.append({"id": step_id, "kind": kind, "config": clean})
    return out


# ── what a step hands back ─────────────────────────────────────────────


def source(label: str, detail: str | None = None, url: str | None = None) -> dict[str, Any]:
    """Where a fact came from, as the run page shows it."""
    return {"label": label[:200], "detail": (detail or None) and detail[:300], "url": url}


@dataclass(slots=True)
class Out:
    headline: str
    output: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: str = "done"  # done | skipped


def skipped(why: str) -> Out:
    return Out(headline=why, status="skipped")


class Stop(Exception):
    """The run ends here, on purpose. The message says why."""


# ── what a run knows ───────────────────────────────────────────────────


@dataclass(slots=True)
class Doc:
    readable: Readable
    origin: str
    name: str
    url: str | None = None


@dataclass(slots=True)
class Context:
    task_id: str | None = None
    task_title: str | None = None
    end_user: str | None = None
    closing: date | None = None
    deadline: date | None = None
    docs: list[Doc] = field(default_factory=list)
    documents_read: bool = False
    #: summary, customer, deadline, conditions, missing, items, how
    reading: dict[str, Any] | None = None
    #: per item index: past matches, best first
    history: dict[int, list[dict[str, Any]]] | None = None
    suppliers: list[dict[str, Any]] = field(default_factory=list)
    offers: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] | None = None
    #: The last "Draft an email" step's drafts, for "Send the emails".
    drafts: list[dict[str, Any]] = field(default_factory=list)
    drafts_step_id: str | None = None
    #: The task's folder in the Proposal Team Channel, once looked up.
    folder_url: str | None = None
    answers: list[str] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)
    notified: bool = False

    @property
    def items(self) -> list[dict[str, Any]]:
        return list((self.reading or {}).get("items") or [])

    def when(self) -> date | None:
        """The date that decides "how long is left": the bid closing date, else the due date."""
        if self.closing:
            return self.closing
        if self.deadline:
            return self.deadline
        text = str((self.reading or {}).get("deadline") or "")
        try:
            return date.fromisoformat(text[:10]) if text else None
        except ValueError:
            return None


# ── helpers ────────────────────────────────────────────────────────────


def _s(value: Any) -> str:
    return str(value or "").strip()


def _dec(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(Decimal("0.01")))


def _qty(value: Any) -> str:
    number = _dec(value)
    if number is None:
        return NOT_GIVEN
    return f"{number.normalize():f}"


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def item_line(item: dict[str, Any]) -> str:
    """An item as one line of an email or a summary."""
    parts = [_s(item.get("description"))]
    if _s(item.get("part_number")):
        parts.append(f"P/N {_s(item['part_number'])}")
    if _s(item.get("brand")):
        parts.append(_s(item["brand"]))
    quantity = _qty(item.get("quantity"))
    unit = _s(item.get("unit"))
    parts.append(f"qty {quantity}{f' {unit}' if unit and quantity != NOT_GIVEN else ''}")
    return " — ".join(parts)


# ── reading item tables without a model ────────────────────────────────

_DESC_HEAD = re.compile(r"\b(description|item|particulars?|material|product|specification|scope|goods|equipment)\b", re.I)
_QTY_HEAD = re.compile(r"\b(qty|qnty|quantity|quantities|nos\.?|no\. ?of)\b", re.I)
_UNIT_HEAD = re.compile(r"\b(unit|uom|u/m)\b", re.I)
_PN_HEAD = re.compile(r"\b(part|p/?n|model|code|cat(alogue)?\.? ?no|mfr|item ?code)\b", re.I)
_BRAND_HEAD = re.compile(r"\b(brand|make|manufacturer|oem)\b", re.I)
_TOTALS = re.compile(r"^\s*(sub ?total|total|grand total|vat|tax|discount)\b", re.I)


def _column(header: list[str], pattern: re.Pattern[str], *, avoid: set[int]) -> int | None:
    for index, cell in enumerate(header):
        if index not in avoid and pattern.search(cell or ""):
            return index
    return None


#: The headers that name the words of an item; "item" alone is as often the line number.
_DESC_STRONG = re.compile(r"\b(description|particulars?|specification|scope|goods)\b", re.I)
_HAS_LETTERS = re.compile(r"[^\W\d_]{2,}")


def _desc_column(header: list[str], rows: list[list[Any]], *, avoid: set[int]) -> int | None:
    """The column that describes each item: one whose cells are words, not line numbers.

    "Item | Description | Qty" is common, and "Item" there is 1, 2, 3 — so a
    real description header wins over "item", and a column whose cells are only
    numbers is never taken, whatever its header says (2026-10-16: a PDF read
    "8", "9", "10" as the items).
    """
    sample = rows[:15]

    def wordy(index: int) -> bool:
        cells = [_s(r[index]) for r in sample if index < len(r) and _s(r[index])]
        return bool(cells) and sum(1 for c in cells if _HAS_LETTERS.search(c)) * 2 >= len(cells)

    candidates = [i for i, cell in enumerate(header) if i not in avoid and _DESC_HEAD.search(cell or "")]
    candidates.sort(key=lambda i: 0 if _DESC_STRONG.search(header[i] or "") else 1)
    return next((i for i in candidates if wordy(i)), None)


def table_items(readable: Readable) -> list[dict[str, Any]]:
    """Item lines from a document's own tables: a description column and a quantity column.

    The plain reading of a bill of quantities. A table without both columns is
    not an item list, and nothing is taken from it.
    """
    items: list[dict[str, Any]] = []
    for t_index, table in enumerate(readable.tables or []):
        for h_index, header in enumerate(table[:6]):
            qty_col = _column(header, _QTY_HEAD, avoid=set())
            if qty_col is None:
                continue
            desc_col = _desc_column(header, table[h_index + 1:], avoid={qty_col})
            if desc_col is None:
                continue
            taken = {qty_col, desc_col}
            unit_col = _column(header, _UNIT_HEAD, avoid=taken)
            pn_col = _column(header, _PN_HEAD, avoid=taken | {unit_col or -1})
            brand_col = _column(header, _BRAND_HEAD, avoid=taken | {unit_col or -1, pn_col or -1})
            for row in table[h_index + 1:]:
                cell = lambda i: _s(row[i]) if i is not None and i < len(row) else ""  # noqa: E731
                description = cell(desc_col)
                quantity = _dec(cell(qty_col))
                if not description or quantity is None or quantity <= 0 or _TOTALS.match(description):
                    continue
                items.append({
                    "description": description[:600],
                    "part_number": cell(pn_col) or None,
                    "brand": cell(brand_col) or None,
                    "quantity": f"{quantity.normalize():f}",
                    "unit": cell(unit_col) or None,
                    "specification": None,
                    "source_document": readable.file_name,
                    "how": f"table {t_index + 1}",
                })
            break
    return items


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One line per item, across documents: the same list in a PDF and a spreadsheet is one list.

    Compared without case, spaces or punctuation, and with the quantity, so
    "VALVE,3-WAY,1/2IN" and "Valve 3-way 1/2in" are one item while two lines of
    the same thing in different quantities stay two.
    """
    seen: set[tuple[str, str, str]] = set()
    out = []
    for item in items:
        flat = lambda v: re.sub(r"[\W_]+", "", _s(v).casefold())  # noqa: E731
        key = (flat(item.get("description")), flat(item.get("part_number")), _qty(item.get("quantity")))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ── the runner ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class Deps:
    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    sharepoint: Any
    drive: Any
    model: AgentModel
    #: The free models (Groq first), asked before Haiku for every text-only step.
    free: TextModel | None = None
    #: Mails the owner when a run needs them or ends — as themselves, to themselves.
    mailer: GraphMailer | None = None
    #: For the Teams card, posted to AGENT_TEAMS_WEBHOOK_URL when it is set.
    http: Any = None
    #: The application's state, for what the RFQ follow-up drives: the mail
    #: reader, the quote extractor, Zoho, the quote mailer (``app.agents.rfqs``).
    state: Any = None


SYSTEM: Final = """\
You are {agent}, an assistant working for {person} at Hamdaz, a trading and \
supply company in the UAE.

Use ONLY the material you are given. Every fact you state must carry the \
label of where it came from, exactly as the material labels it (for example \
[document: RFQ.pdf] or [task]). If the material does not say something, write \
"not given" — never guess, and never fill in from general knowledge. Be brief.
{instructions}"""


class Runner:
    def __init__(
        self, deps: Deps, run_id: uuid.UUID, uploads: list[tuple[str, bytes, str | None]], *, resume: bool = False
    ) -> None:
        self.deps = deps
        self.run_id = run_id
        self.uploads = uploads
        #: Started again after a restart cut it off: the question it was
        #: waiting on is kept, and not asked twice.
        self.resume = resume
        self.ctx = Context()
        self.answer: dict[str, Any] | None = None
        self.answered = asyncio.Event()
        self.stopping = False
        self.session: AsyncSession
        self.run: AgentRun
        self.user: User
        self.agent_name = "Your agent"
        self.instructions = ""
        #: The agent's picture for Teams cards, once one has been saved.
        self.face: str | None = None
        self.task: asyncio.Task[None] | None = None

    # ── control from the routes ──

    def give_answer(self, answer: dict[str, Any]) -> None:
        """An answer from the routes. They have written it to the run as well."""
        self.answer = answer
        self.answered.set()

    def stop(self) -> None:
        self.stopping = True
        self.answer = {"go": False, "text": "", "stop": True}
        self.answered.set()

    # ── the run ──

    async def go(self) -> None:
        async with self.deps.factory() as session:
            run = await session.get(AgentRun, self.run_id)
            user = await session.get(User, run.owner_id) if run else None
            if run is None or user is None:
                return
            self.session, self.run, self.user = session, run, user
            agent = await session.get(UserAgent, run.agent_id)
            self.face = face_url(self.deps.settings, agent)
            try:
                await self._go()
            except Exception as exc:  # noqa: BLE001 — a run must end in a state, whatever happened
                logger.exception("agent run %s failed", self.run_id)
                await session.rollback()
                run = await session.get(AgentRun, self.run_id)
                if run is not None:
                    run.status = "failed"
                    run.error = f"The run stopped on an error: {type(exc).__name__}: {exc}"[:1000]
                    run.summary = run.summary or "The run stopped on an error before it finished."
                    run.finished_at = datetime.now(UTC)
                    await session.commit()

    async def _go(self) -> None:
        run, ctx = self.run, self.ctx
        plan = run.plan or {}
        self.agent_name = _s(plan.get("name")) or "Your agent"
        self.instructions = _s(plan.get("instructions"))
        steps: list[dict[str, Any]] = list(plan.get("steps") or [])
        results = [
            {"step_id": s["id"], "kind": s["kind"], "name": KINDS.get(s["kind"], (s["kind"], ""))[0],
             "status": "pending", "headline": None, "output": {}, "sources": [], "notes": [], "ai": None}
            for s in steps
        ]
        run.results = results
        run.status, run.model = "running", self.deps.model.model
        run.started_at = run.started_at or datetime.now(UTC)
        run.heartbeat_at = datetime.now(UTC)
        await self._load_task()
        await self.session.commit()

        stopped: str | None = None
        for index, step in enumerate(steps):
            if self.stopping:
                stopped = "Stopped by you."
            if stopped:
                results[index]["status"] = "stopped"
                continue
            result = results[index]
            result["status"], result["started_at"] = "running", _now()
            self._write(results)
            run.heartbeat_at = datetime.now(UTC)
            await self.session.commit()

            spent = Spent()
            began = time.monotonic()
            try:
                out = await self._step(step, spent, results, index)
            except Stop as why:
                stopped = str(why)
                out = Out(headline=stopped, status="stopped")
            except LLMError as exc:
                out = Out(headline=str(exc), status="failed")
            except Exception as exc:  # noqa: BLE001 — one step failing should not lose the rest
                logger.exception("agent step %s (%s) failed", step["id"], step["kind"])
                out = Out(headline=f"This step failed: {type(exc).__name__}: {exc}"[:500], status="failed")

            result.update(
                status=out.status, headline=out.headline, output=out.output, sources=out.sources,
                notes=out.notes, finished_at=_now(), seconds=round(time.monotonic() - began, 1),
            )
            if spent.free_by and not (spent.input_tokens or spent.output_tokens or spent.searches):
                result["ai"] = {
                    "model": spent.free_by, "free": True, "input_tokens": 0,
                    "output_tokens": 0, "searches": 0, "cost_usd": "0",
                }
            elif spent.input_tokens or spent.output_tokens or spent.searches:
                cost = spent.cost(self.deps.model.model)
                result["ai"] = {
                    "model": self.deps.model.model, "input_tokens": spent.input_tokens,
                    "output_tokens": spent.output_tokens, "searches": spent.searches, "cost_usd": str(cost),
                }
                run.input_tokens += spent.input_tokens
                run.output_tokens += spent.output_tokens
                run.web_searches += spent.searches
                run.cost_usd = (run.cost_usd or Decimal(0)) + cost
            if out.status == "done" and KINDS[step["kind"]][1] != "start":
                ctx.headlines.append(out.headline)
            if step["kind"] == "draft_email" and out.status == "done":
                ctx.drafts_step_id = step["id"]
            # Only this step's own question: one kept from before a restart
            # belongs to a step still ahead.
            if (run.question or {}).get("step_id") == step["id"]:
                run.question = None
            run.heartbeat_at = datetime.now(UTC)
            self._write(results)
            await self.session.commit()

        await self._finish(stopped)

    def _write(self, results: list[dict[str, Any]]) -> None:
        # A fresh list, so SQLAlchemy sees the JSON column change.
        self.run.results = [dict(r) for r in results]

    async def _finish(self, stopped: str | None) -> None:
        run, ctx = self.run, self.ctx
        failed = sum(1 for r in run.results if r["status"] == "failed")
        where = f" on “{ctx.task_title}”" if ctx.task_title else ""
        if stopped:
            run.status = "stopped"
            run.summary = f"{self.agent_name} stopped{where}: {stopped}"
        else:
            run.status = "done"
            done = "; ".join(ctx.headlines[-6:]) or "nothing to report"
            run.summary = f"{self.agent_name} finished{where}. {done}."
            if failed:
                run.summary += f" {failed} step(s) could not finish — see the run."
        run.finished_at = datetime.now(UTC)
        run.question = None
        # Every run ends somewhere its owner will see it.
        if not ctx.notified:
            await notify(
                self.session, users=[self.user.id], kind=NotificationKind.GENERAL,
                title=f"{self.agent_name}: {'stopped' if stopped else 'finished'}{where}",
                body=run.summary[:1000], link=self.link(), source="agent_run", source_id=str(run.id),
            )
        await self.session.commit()
        points = [
            f"{r['name']}: {r['headline']}" for r in run.results
            if r.get("headline") and r["status"] in ("done", "failed") and KINDS.get(r["kind"], ("", ""))[1] != "start"
        ]
        # And in Teams, for when they are not in the app.
        await tell_in_teams(
            self.deps, self.user, run, agent=self.agent_name,
            title=f"{self.agent_name} {'stopped' if stopped else 'finished'}{where}",
            lead=stopped or ("Here is what I did." if points else "Nothing to report."),
            points=points, button="See the results", face=self.face,
        )
        await self.email_owner(
            subject=f"{self.agent_name} {'stopped' if stopped else 'finished'}{where}",
            lead=run.summary or "", points=points, button="See the results",
        )

    def link(self) -> str:
        return run_link(self.run)

    async def email_owner(self, *, subject: str, lead: str, points: list[str], button: str) -> None:
        """Mail the owner as well — only when AGENT_EMAIL_NOTIFY is on. Teams is the usual way.

        Never fails the run: it is a copy of a notification already in the bell.
        """
        deps = self.deps
        if deps.mailer is None or not deps.settings.agent_email_notify:
            return
        if not self.user.email or not getattr(self.user, "entra_object_id", None):
            return
        url = f"{deps.settings.frontend_url.rstrip('/')}{self.link()}"
        items = "".join(f"<li style='margin:4px 0'>{_html(p)}</li>" for p in points[:12])
        html = (
            "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#111;max-width:620px'>"
            f"<p style='margin:0 0 12px'>{_html(lead)}</p>"
            + (f"<ul style='padding-left:18px;margin:0 0 16px;color:#333'>{items}</ul>" if items else "")
            + f"<p><a href='{url}' style='display:inline-block;background:#111;color:#fff;padding:9px 16px;"
            f"border-radius:10px;text-decoration:none'>{_html(button)}</a></p>"
            "<p style='color:#888;font-size:12px'>From your agent in the Hamdaz app. Nothing is sent for "
            "you unless you say yes.</p></div>"
        )
        try:
            await deps.mailer.send(sender=self.user.entra_object_id, recipients=[self.user.email], subject=subject[:200], html=html)
        except Exception:  # noqa: BLE001 — a mail that fails must not fail the run
            logger.warning("agent run %s: the owner could not be mailed", self.run.id, exc_info=True)

    async def ask_in_teams(self, question: dict[str, Any], results: list[dict[str, Any]], index: int) -> bool:
        """Ask the owner in Teams too, through their Power Automate flow. True if it went.

        The flow posts ``card`` to ``recipient`` with "Post adaptive card and
        wait for a response", then POSTs the answer to ``callback_url`` with
        ``run_id``, ``step_id`` and ``token`` handed back unchanged, ``go`` and
        ``text`` from the card, and ``data`` — the card's whole response,
        which carries the ticks on a "send these emails?" card. The token is
        this run's and this question's alone (see ``answer_token``), so the
        callback cannot answer anything else. Never fails the run.
        """
        step_id = results[index]["step_id"]
        about = next((r for r in results[:index] if r["step_id"] == question.get("about_step_id")), None) or next(
            (r for r in reversed(results[:index]) if r["status"] == "done"), None
        )
        link = f"{self.deps.settings.frontend_url.rstrip('/')}{self.link()}"
        mode = question.get("mode")
        if mode == "docs":
            card = docs_card(agent=self.agent_name, task=self.ctx.task_title, closing=self.ctx.when(),
                             question=question, link=link, face=self.face)
        elif mode == "send":
            card = send_card(agent=self.agent_name, task=self.ctx.task_title, question=question, link=link,
                             face=self.face)
        else:
            card = question_card(agent=self.agent_name, task=self.ctx.task_title, question=question, about=about,
                                 link=link, face=self.face)
        return await post_card(
            self.deps, self.user, self.run, step_id=step_id, title=str(question.get("title") or "Carry on?"),
            agent=self.agent_name, task=self.ctx.task_title, card=card,
        )

    async def ask_json(
        self, *, system: str, text: str, schema: dict[str, Any], spent: Spent, max_tokens: int = 4000,
        effort: str = "medium",
    ) -> dict[str, Any]:
        """One JSON answer: a free model first, Haiku only when every free one is busy.

        Text-only steps — summaries, wording, judgement over the material —
        come here. Reading a scan and searching the web need Claude itself and
        go to ``model.ask`` / ``model.search`` directly.
        """
        if self.deps.free is not None:
            # A filled-in example, not the schema: shown the schema, the free
            # models answer with the schema ({"text": {"type": "string"}}).
            shaped = (
                f"{system}\n\nReply with one JSON object and nothing else, shaped like this example "
                f"(write line breaks inside strings as \\n):\n{json.dumps(_example(schema))}"
            )
            found = await self.deps.free.ask(
                system=shaped, user=text, max_tokens=max_tokens, free_only=True, label="agent step",
                reasoning_effort=effort, check=lambda p: _fits(p, schema),
            )
            if found is not None:
                spent.free_by = found.who
                return found.payload
        if not self.deps.model.configured:
            raise LLMError("The free models are busy and no Anthropic key is set, so this step could not use AI.")
        return await self.deps.model.ask(
            system=system, content=[{"type": "text", "text": text}], schema=schema,
            user_key=self.user_key, spent=spent, max_tokens=max_tokens,
        )

    async def _load_task(self) -> None:
        ctx, run = self.ctx, self.run
        if not run.task_id:
            return
        ctx.task_id = run.task_id
        row = await self.session.get(ProposalIndexItem, run.task_id)
        if row is not None and not row.deleted:
            ctx.task_title, ctx.end_user = row.title, row.end_user
            ctx.closing, ctx.deadline = row.bid_closing_date, row.deadline or row.due_date
            return
        try:
            task = await self.deps.sharepoint.task(run.task_id)
        except SharePointError:
            ctx.task_title = run.task_title
            return
        ctx.task_title, ctx.end_user, ctx.closing = task.title, task.end_user, task.closing_date

    # ── waiting for the owner ──

    async def wait_for_answer(
        self,
        question: dict[str, Any],
        results: list[dict[str, Any]],
        index: int,
        *,
        give_up_after: timedelta = ANSWER_TIMEOUT,
        timeout_message: str = "No answer came within a day.",
        poll: Any = None,
        poll_every: float = 120,
    ) -> dict[str, Any]:
        """Ask the owner, in the app and in Teams, and wait for the answer.

        The answer comes from whichever is first: the app or the Teams flow
        (handed over directly when they reach this process, and written to
        ``run.question.answer`` either way), or ``poll`` — a check of its own
        that can answer for the owner, e.g. documents turning up in the task's
        folder. A question already asked before a restart is not asked again.
        """
        if self.stopping:
            raise Stop("Stopped by you.")
        run = self.run
        step_id = results[index]["step_id"]
        self.answer = None
        self.answered.clear()
        results[index]["status"] = "waiting"
        run.status = "waiting"
        held = run.question or {}
        again = held.get("step_id") == step_id
        if again:
            run.question = {**held, **question, "step_id": step_id}
        else:
            run.question = {**question, "step_id": step_id, "asked_at": _now()}
        run.heartbeat_at = datetime.now(UTC)
        self._write(results)
        if not again:
            await notify(
                self.session, users=[self.user.id], kind=NotificationKind.GENERAL,
                title=f"{self.agent_name} needs your answer", body=str(question.get("title") or "")[:500],
                link=self.link(), source="agent_wait", source_id=f"{run.id}:{index}:{run.question['asked_at']}",
            )
        await self.session.commit()
        if not again:
            await self.email_owner(
                subject=f"{self.agent_name} needs your answer{f' — {self.ctx.task_title}' if self.ctx.task_title else ''}",
                lead=f"{question.get('title') or 'Carry on?'} {question.get('message') or ''}".strip(),
                points=[f"{r['name']}: {r['headline']}" for r in results[:index] if r.get("headline") and r["status"] == "done"],
                button="Answer it",
            )
            # The answer may come back from Teams instead, through /agents/teams/answer.
            if await self.ask_in_teams(question, results, index):
                run.question = {**run.question, "teams": True}
                await self.session.commit()

        asked = parse_time(run.question.get("asked_at")) or datetime.now(UTC)
        until = asked + give_up_after
        beat = polled = time.monotonic()
        answer: dict[str, Any] | None = None
        while answer is None:
            if self.answered.is_set():
                answer = self.answer or {"go": False, "text": ""}
                break
            # Written by another process, or before a restart.
            await self.session.refresh(run, ["question"])
            # Ended at once: a wait can last days, and must not hold a connection.
            await self.session.commit()
            held = run.question or {}
            if held.get("step_id") == step_id and isinstance(held.get("answer"), dict):
                answer = held["answer"]
                break
            if datetime.now(UTC) >= until:
                raise Stop(timeout_message)
            now = time.monotonic()
            if poll is not None and now - polled >= poll_every:
                polled = now
                try:
                    answer = await poll()
                except Exception:  # noqa: BLE001 — a check that fails is tried again
                    logger.warning("agent run %s: a check while waiting failed", run.id, exc_info=True)
                if answer is not None:
                    break
            if now - beat >= HEARTBEAT_SECONDS:
                beat = now
                run.heartbeat_at = datetime.now(UTC)
                await self.session.commit()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.answered.wait(), ANSWER_POLL_SECONDS)
        run.status = "running"
        run.heartbeat_at = datetime.now(UTC)
        if self.stopping or answer.get("stop"):
            self.stopping = True
            raise Stop("Stopped by you.")
        return answer

    # ── the material a model sees ──

    def material(self) -> str:
        """What the run knows, every fact labelled with where it came from."""
        ctx = self.ctx
        lines: list[str] = []
        if ctx.task_title:
            lines.append(f"[task] Title: {ctx.task_title}")
            lines.append(f"[task] End user: {ctx.end_user or NOT_GIVEN}")
            lines.append(f"[task] Bid closing date: {_iso(ctx.closing) or NOT_GIVEN}")
            lines.append(f"[task] Due date: {_iso(ctx.deadline) or NOT_GIVEN}")
        if ctx.reading:
            r = ctx.reading
            if r.get("summary"):
                lines.append(f"[documents] Summary: {r['summary']}")
            for item in ctx.items:
                lines.append(f"[document: {item.get('source_document') or 'unknown'}] Item: {item_line(item)}")
            for condition in r.get("conditions") or []:
                lines.append(f"[documents] Condition: {condition}")
        if ctx.history:
            for i, matches in ctx.history.items():
                for m in matches[:2]:
                    lines.append(f"[{m['label']}] For item {i + 1}: {m['description']} at {m.get('rate') or NOT_GIVEN} {m.get('currency') or ''}")
        for s in ctx.suppliers:
            lines.append(f"[{s['source']['label']}] Supplier: {s['name']}, covers {', '.join(s.get('items_covered') or []) or NOT_GIVEN}")
        if ctx.tasks:
            for t in ctx.tasks[:40]:
                lines.append(f"[Proposals list] Task: {t['title']} — deadline {t.get('deadline') or NOT_GIVEN}, status {t.get('status') or NOT_GIVEN}")
        for answer in ctx.answers:
            lines.append(f"[your answer] {answer}")
        text = "\n".join(lines) or "(nothing yet)"
        return reading.trim(text, MATERIAL_BUDGET)

    def system(self) -> str:
        extra = f"\nWhat {self.user.display_name} asked of you, in their words:\n{self.instructions}" if self.instructions else ""
        return SYSTEM.format(agent=self.agent_name, person=self.user.display_name, instructions=extra)

    @property
    def user_key(self) -> str:
        return str(self.user.id)

    # ── the steps ──

    async def _step(self, step: dict[str, Any], spent: Spent, results: list[dict[str, Any]], index: int) -> Out:
        kind, cfg = step["kind"], step.get("config") or {}
        if KINDS[kind][1] == "start":
            return self.start(kind)
        handler = getattr(self, f"s_{kind}", None)
        if handler is None:
            return skipped("This block cannot run yet.")
        if kind in ("ask_me", "get_approval", "check", "ask_documents", "send_emails"):
            return await handler(cfg, spent, results, index)
        return await handler(cfg, spent)

    def start(self, kind: str) -> Out:
        who = self.user.display_name
        where = f" on “{self.ctx.task_title}”" if self.ctx.task_title else ""
        if self.run.started_by == "trigger":
            out = Out(headline=f"Started on its own: “{self.ctx.task_title or 'a task'}” was assigned to {who}")
        else:
            out = Out(headline=f"Started by {who}{where}")
            if kind not in ("start_manual", "start_new_task"):
                out.notes.append(f"“{KINDS[kind][0]}” does not start it on its own yet. This run was started by hand.")
        if self.ctx.task_title:
            out.sources.append(source("Proposals list", self.ctx.task_title))
        return out

    async def _task_documents(self) -> tuple[list[tuple[str, str, str | None, Any]], list[str]]:
        """The task's files — list attachments and its channel folder: (name, origin, url, fetch), notes."""
        ctx, deps = self.ctx, self.deps
        found: list[tuple[str, str, str | None, Any]] = []
        notes: list[str] = []
        if not ctx.task_id:
            return found, notes
        try:
            for att in await deps.sharepoint.attachments_of(ctx.task_id):
                name = att["file_name"]
                found.append((name, "task attachment", None, ("attachment", name)))
        except SharePointConsentError:
            notes.append("The task's list attachments could not be read (the app lacks SharePoint permission); its folder was read instead.")
        except SharePointError as exc:
            notes.append(f"The task's attachments could not be read: {exc}")
        if deps.drive.enabled and ctx.task_title:
            try:
                folder = await deps.drive.find_task_folder(ctx.task_title)
                if folder.how != "created":
                    ctx.folder_url = ctx.folder_url or folder.web_url
                    for f in await deps.drive.list_files(folder.name):
                        if reading.is_own_output(f["name"], f["path"], deps.settings.enquiry_report_folder):
                            continue
                        found.append((f["name"], f"task folder: {f['path']}", f.get("web_url"), ("drive", f["id"])))
                else:
                    notes.append("The task has no folder in the Proposal Team Channel yet.")
            except DriveError as exc:
                notes.append(f"The task folder could not be read: {exc}")
        return found, notes

    async def _folder_link(self) -> str | None:
        """The task's channel folder, made if it is not there yet — somewhere to put the documents."""
        ctx, drive = self.ctx, self.deps.drive
        if ctx.folder_url or not drive.enabled or not ctx.task_title:
            return ctx.folder_url
        try:
            folder = await drive.find_task_folder(ctx.task_title)
            ctx.folder_url = folder.web_url if folder.how != "created" else await drive.ensure_folder(folder.name)
        except DriveError as exc:
            logger.info("agent run %s: no folder link (%s)", self.run.id, exc)
        return ctx.folder_url

    async def s_ask_documents(self, cfg: dict[str, Any], spent: Spent, results: list[dict[str, Any]], index: int) -> Out:
        """Make sure the task has its documents: start at once if it does, else ask for them and wait.

        The owner is asked in Teams (and in the app) to put the RFQ or tender in
        the task's channel folder; the folder is looked at every couple of
        minutes meanwhile, so dropping the files in is answer enough.
        """
        ctx = self.ctx
        if self.uploads:
            return Out(headline=f"{len(self.uploads)} file(s) were added to this run — starting on those")
        if not ctx.task_id:
            return skipped("No task was chosen, so there were no documents to ask for.")
        found, notes = await self._task_documents()
        folder_url = await self._folder_link()
        folder_source = [source("task folder", ctx.task_title, folder_url)] if folder_url else []
        if found:
            names = [f[0] for f in found]
            if self.run.started_by == "trigger":
                await tell_in_teams(
                    self.deps, self.user, self.run, agent=self.agent_name,
                    title=f"On it: {ctx.task_title}",
                    lead=f"“{ctx.task_title}” was just assigned to you. I found {len(found)} document(s) on it and I'm starting now — I'll come back to you before anything is sent.",
                    points=names[:8], button="Watch me work", face=self.face,
                )
            return Out(
                headline=f"{len(found)} document(s) already on the task — started straight away",
                output={"files": names, "folder_url": folder_url}, sources=folder_source, notes=notes,
            )

        days = max(1, min(int(_dec(cfg.get("days")) or 3), 14))

        async def documents_arrived() -> dict[str, Any] | None:
            now_found, _ = await self._task_documents()
            return {"go": True, "text": "", "found": len(now_found)} if now_found else None

        where = "the task's folder in the Proposal Team Channel" if folder_url else "the task (attach them to it in the Proposals list)"
        message = (
            f"I'll start working on it as soon as they're there: put the RFQ or tender files in {where}, "
            "then press “They're in”. I look in the folder every couple of minutes, so dropping them in is enough."
        )
        nudged = False
        for _ in range(3):
            answer = await self.wait_for_answer(
                {"title": f"Can you share the documents for “{ctx.task_title}”?",
                 "message": ("I still can't see any documents on the task. " if nudged else "") + message,
                 "mode": "docs", "folder_url": folder_url},
                results, index, give_up_after=timedelta(days=days), poll=documents_arrived,
                timeout_message=f"No documents came within {days} day(s), so I stopped. Run me by hand once they're in.",
            )
            if not answer.get("go"):
                raise Stop("You said this task does not need me.")
            if answer.get("text"):
                ctx.answers.append(str(answer["text"]))
            found, notes = await self._task_documents()
            if found:
                return Out(
                    headline=f"{len(found)} document(s) arrived — starting",
                    output={"files": [f[0] for f in found], "folder_url": folder_url},
                    sources=folder_source + [source("your answer")] if answer.get("text") else folder_source,
                    notes=notes,
                )
            # "They're in" pressed, and nothing there yet: ask once more.
            self.run.question = None
            nudged = True
        raise Stop("The documents never reached the task.")

    async def s_read_documents(self, cfg: dict[str, Any], spent: Spent) -> Out:
        ctx, deps = self.ctx, self.deps
        if not ctx.task_id and not self.uploads:
            return skipped("No task was chosen and no files were added, so there was nothing to read.")
        which = _s(cfg.get("which")) or "all"
        found, notes = await self._task_documents()

        wanted = {"pdf": (".pdf",), "excel": (".xlsx", ".xls", ".xlsm", ".csv")}.get(which)
        files_out: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        for name, origin, url, fetch in found:
            if wanted and not name.lower().endswith(wanted):
                continue
            entry = {"name": name, "origin": origin, "url": url, "status": "read", "note": None, "kind": None, "chars": 0}
            if len(ctx.docs) >= MAX_DOCUMENTS:
                entry.update(status="skipped", note=f"Only {MAX_DOCUMENTS} documents are read per run.")
                files_out.append(entry)
                continue
            try:
                if fetch[0] == "attachment":
                    content = await deps.sharepoint.attachment_content(ctx.task_id, fetch[1])
                else:
                    content = await deps.drive.download(fetch[1])
                readable = await asyncio.to_thread(prepare, name, content, None, ocr=False)
            except DocumentError as exc:
                entry.update(status="skipped", note=str(exc))
            except (SharePointError, SharePointConsentError, DriveError) as exc:
                entry.update(status="failed", note=f"Could not download it: {exc}")
            else:
                ctx.docs.append(Doc(readable, origin, name, url))
                entry.update(kind="text" if readable.kind == "text" else "scan", chars=len(readable.text or ""))
                sources.append(source(f"document: {name}", origin, url))
            files_out.append(entry)

        for name, content, content_type in self.uploads:
            entry = {"name": name, "origin": "added to this run", "url": None, "status": "read", "note": None, "kind": None, "chars": 0}
            try:
                readable = await asyncio.to_thread(prepare, name, content, content_type, ocr=False)
            except DocumentError as exc:
                entry.update(status="skipped", note=str(exc))
            else:
                ctx.docs.append(Doc(readable, "added to this run", name))
                entry.update(kind="text" if readable.kind == "text" else "scan", chars=len(readable.text or ""))
                sources.append(source(f"document: {name}", "added to this run"))
            files_out.append(entry)

        ctx.documents_read = True
        read = sum(1 for f in files_out if f["status"] == "read")
        if not files_out:
            return Out(headline="The task has no documents to read", output={"files": []}, notes=notes + [
                "Attach the RFQ or tender to the task, or add files when you start the run."])
        return Out(
            headline=f"Read {read} of {len(files_out)} document(s) — no AI used",
            output={"files": files_out}, sources=sources, notes=notes,
        )

    async def s_extract_requirements(self, cfg: dict[str, Any], spent: Spent) -> Out:
        ctx, session = self.ctx, self.session
        # 1. An enquiry analysis already read this task: take its lines, free.
        if ctx.task_id and not self.uploads:
            analysis = await session.scalar(select(EnquiryAnalysis).where(EnquiryAnalysis.task_id == ctx.task_id))
            if analysis is not None:
                # Loaded explicitly: a lazy load inside async is a MissingGreenlet.
                await session.refresh(analysis, ["lines"])
            if analysis is not None and analysis.status == "done" and analysis.lines:
                items = [
                    {"description": line.description, "part_number": line.part_number, "brand": line.brand,
                     "quantity": str(line.quantity) if line.quantity is not None else None, "unit": line.unit,
                     "specification": line.specification, "source_document": line.source_document,
                     "how": "enquiry analysis"}
                    for line in sorted(analysis.lines, key=lambda l: l.position)
                ]
                ctx.reading = {
                    "summary": analysis.summary or NOT_GIVEN, "customer": analysis.customer or ctx.end_user or NOT_GIVEN,
                    "deadline": analysis.deadline or _iso(ctx.closing) or NOT_GIVEN,
                    "conditions": list(analysis.conditions or []), "missing": list(analysis.missing or []),
                    "items": items, "how": {"enquiry": len(items), "tables": 0, "ai": 0},
                }
                when = analysis.finished_at.date().isoformat() if analysis.finished_at else "earlier"
                return Out(
                    headline=f"{len(items)} item(s), taken from the task's enquiry analysis ({when}) — no AI used",
                    output=ctx.reading,
                    sources=[source("Enquiry analysis", f"of {when}", f"/enquiries/{ctx.task_id}")]
                    + _item_sources(items),
                )

        if not ctx.documents_read:
            await self.s_read_documents({}, spent)
        requirement_docs = [d for d in ctx.docs if reading.guess_kind(d.name) != "supplier_quote"]
        if not requirement_docs:
            return skipped("There were no requirement documents to read.")

        # 2. The documents' own tables: a bill of quantities reads itself.
        items: list[dict[str, Any]] = []
        leftovers: list[Doc] = []
        for doc in requirement_docs:
            found = table_items(doc.readable)
            items.extend(found)
            if not found:
                leftovers.append(doc)
        items = _dedupe(items)
        from_tables = len(items)

        result: dict[str, Any] = {
            "summary": NOT_GIVEN, "customer": ctx.end_user or NOT_GIVEN,
            "deadline": _iso(ctx.closing) or NOT_GIVEN, "conditions": [], "missing": [],
        }
        notes: list[str] = []
        # 3. What the tables did not cover, read by the model — if allowed.
        use_ai = cfg.get("ai", True) is not False
        ai_count = 0
        scans = [d for d in leftovers if d.readable.kind != "text"]
        can_read = self.deps.free is not None if not scans else self.deps.model.configured
        if leftovers and use_ai and (can_read or self.deps.model.configured):
            context = (
                f"Task: {ctx.task_title or NOT_GIVEN}. End user: {ctx.end_user or NOT_GIVEN}. "
                f"Items already read from tables: {from_tables}."
            )
            prepared = reading.parts_for([(d.readable, d.origin) for d in leftovers], context=context)
            extra = _s(cfg.get("notes"))
            instructions = reading.INSTRUCTIONS + (f"\n\nAlso: {extra}" if extra else "") + (
                "\n\nIf a detail is not in the documents, leave it empty — never guess.")
            if scans:
                # A scan is pages, not text: only Claude reads those.
                payload = await self.deps.model.ask(
                    system=instructions, content=prepared.parts, schema=reading.SCHEMA,
                    user_key=self.user_key, spent=spent, max_tokens=32_000,
                )
            else:
                text = "\n\n".join(p["text"] for p in prepared.parts if p.get("type") == "text")
                payload = await self.ask_json(
                    system=instructions, text=text, schema=reading.SCHEMA, spent=spent, max_tokens=16_000,
                )
            parsed = reading.parse(_json(payload))
            for req in parsed.items:
                items.append({
                    "description": req.description, "part_number": req.part_number, "brand": req.brand,
                    "quantity": str(req.quantity) if req.quantity is not None else None, "unit": req.unit,
                    "specification": req.specification, "source_document": req.source_document, "how": "read by AI",
                })
            items = _dedupe(items)
            ai_count = len(items) - from_tables
            result.update(
                summary=parsed.summary or NOT_GIVEN, customer=parsed.customer or result["customer"],
                deadline=parsed.deadline or result["deadline"], conditions=parsed.conditions, missing=parsed.missing,
            )
            notes.extend(prepared.left_out)
        elif leftovers:
            names = ", ".join(d.name for d in leftovers[:5])
            notes.append(
                f"Not read: {names} — no item table in them"
                + (" and AI reading is off for this step." if not use_ai else " and no model is configured.")
            )
        if not items:
            ctx.reading = {**result, "items": [], "how": {"tables": 0, "ai": 0}}
            return Out(headline="No items found in the documents", output=ctx.reading, notes=notes)

        result["items"] = items
        result["how"] = {"tables": from_tables, "ai": ai_count}
        ctx.reading = result
        how = f"{from_tables} from the documents' tables" + (f", {ai_count} read by AI" if ai_count else " — no AI used")
        return Out(headline=f"{len(items)} item(s): {how}", output=result, sources=_item_sources(items), notes=notes)

    async def s_lookup_tasks(self, cfg: dict[str, Any], spent: Spent) -> Out:
        which = _s(cfg.get("which")) or "due_week"
        email = (self.user.email or "").lower()
        rows = (await self.session.scalars(
            select(ProposalIndexItem)
            .where(func.lower(ProposalIndexItem.assigned_email) == email)
            .where(ProposalIndexItem.is_open.is_(True), ProposalIndexItem.deleted.is_(False))
        )).all()
        today = date.today()
        tasks = []
        for row in rows:
            when = row.bid_closing_date or row.deadline or row.due_date
            days = (when - today).days if when else None
            if which == "overdue" and (days is None or days >= 0):
                continue
            if which == "due_week" and (days is None or days > 7):
                continue
            tasks.append({
                "id": row.item_id, "title": row.title, "deadline": _iso(when), "days_left": days,
                "status": row.effective_status or row.status, "end_user": row.end_user,
            })
        tasks.sort(key=lambda t: (t["days_left"] is None, t["days_left"] if t["days_left"] is not None else 0))
        self.ctx.tasks = tasks
        label = {"due_week": "due within a week or late", "overdue": "late", "open": "open"}.get(which, "open")
        return Out(
            headline=f"{len(tasks)} task(s) of yours {label} — no AI used",
            output={"tasks": tasks}, sources=[source("Proposals list", "your open tasks, as last synced")],
        )

    async def _history(self, items: list[dict[str, Any]], since_months: int | None) -> dict[int, list[dict[str, Any]]]:
        rows = await matching.load_pool(self.session, task_id=self.ctx.task_id or "", comparison_id=None)
        pool = matching.Pool(rows)
        cutoff = date.today() - timedelta(days=30 * since_months) if since_months else None
        found: dict[int, list[dict[str, Any]]] = {}
        for i, item in enumerate(items):
            matches = []
            for value, row in pool.match(_s(item.get("description")), item.get("part_number"), item.get("brand")):
                if cutoff and row.when and row.when < cutoff:
                    continue
                if row.source == "supplier_quote":
                    label = f"supplier quote: {row.supplier or row.counterparty or 'unknown'}"
                else:
                    label = f"our quote {row.ref}"
                matches.append({
                    "source": row.source, "label": label, "ref": row.ref, "date": _iso(row.when),
                    "counterparty": row.counterparty, "supplier": row.supplier,
                    "description": row.description[:300], "part_number": row.part_number,
                    "rate": _money(row.rate), "cost_rate": _money(row.cost_rate), "currency": row.currency,
                    "score": round(value, 2),
                })
                if len(matches) >= 5:
                    break
            if matches:
                found[i] = matches
        return found

    async def s_lookup_history(self, cfg: dict[str, Any], spent: Spent) -> Out:
        ctx = self.ctx
        wanted = _s(cfg.get("for"))
        items = [{"description": wanted}] if wanted else ctx.items
        if not items:
            return skipped("No items to look up — add a step that pulls out the items first, or say what to look for.")
        since = {"6m": 6, "1y": 12, "3y": 36}.get(_s(cfg.get("since")) or "1y")
        history = await self._history(items, since)
        if not wanted:
            ctx.history = history
        rows = [{"item": item_line(items[i]), "matches": m} for i, m in history.items()]
        sources = [source(m["label"], f"{m['ref']} · {m['date'] or 'no date'}") for r in rows for m in r["matches"][:1]]
        return Out(
            headline=f"Found past quotes for {len(history)} of {len(items)} item(s) — no AI used",
            output={"items": rows, "unmatched": [item_line(it) for i, it in enumerate(items) if i not in history]},
            sources=sources[:30],
        )

    async def s_read_supplier_quotes(self, cfg: dict[str, Any], spent: Spent) -> Out:
        return skipped("Reading supplier replies comes with the RFQ mail step, which is not built yet. Use Comparisons for now.")

    async def s_find_suppliers(self, cfg: dict[str, Any], spent: Spent) -> Out:
        ctx = self.ctx
        items = ctx.items
        if not items:
            return skipped("No items to find suppliers for — pull out the items first.")
        count = max(1, min(int(_dec(cfg.get("count")) or 5), 15))
        if ctx.history is None:
            ctx.history = await self._history(items, None)

        # 1. Our own supplier quotes: who has offered these before.
        by_supplier: dict[str, dict[str, Any]] = {}
        for i, matches in ctx.history.items():
            for m in matches:
                if m["source"] != "supplier_quote" or not m.get("supplier"):
                    continue
                s = by_supplier.setdefault(m["supplier"], {"name": m["supplier"], "items_covered": [], "last_quoted": None, "from": "history"})
                label = _s(items[i].get("description"))[:80]
                if label not in s["items_covered"]:
                    s["items_covered"].append(label)
                if m["date"] and (s["last_quoted"] is None or m["date"] > s["last_quoted"]):
                    s["last_quoted"] = m["date"]
        # Most items covered first, then the most recently quoted.
        suppliers = sorted(
            by_supplier.values(), key=lambda s: (len(s["items_covered"]), s["last_quoted"] or ""), reverse=True
        )[:count]
        for s in suppliers:
            latest = await self.session.scalar(
                select(SupplierQuote).where(SupplierQuote.supplier_name == s["name"]).order_by(SupplierQuote.created_at.desc()).limit(1)
            )
            details = (latest.details or {}) if latest else {}
            emails = details.get("emails")
            s["email"] = (emails[0] if isinstance(emails, list) and emails else _s(emails)) or None
            s["phone"] = _s(details.get("phone") or details.get("mobile")) or None
            s["contact_person"] = _s(details.get("contact_person") or (latest.contact if latest else "")) or None
            s["place"] = _s(details.get("country")) or None
            s["website"] = None
            s["source"] = source(f"supplier quote: {s['name']}", f"last quoted {s['last_quoted'] or 'on an unknown date'}")

        notes: list[str] = []
        # 2. The web, only when this step asks and the setting allows.
        web_found = 0
        if cfg.get("web") and len(suppliers) < count:
            if not self.deps.model.web_allowed:
                notes.append("Web search is switched off for agents (AGENT_WEB_SEARCH).")
            elif not self.deps.model.configured:
                notes.append("Web search needs the model, and no Anthropic key is set.")
            else:
                where = {"uae": "the UAE", "gcc": "the GCC", "world": "anywhere"}.get(_s(cfg.get("where")) or "uae", "the UAE")
                wanted_items = "\n".join(f"{n + 1}. {item_line(it)}" for n, it in enumerate(items[:25]))
                known = ", ".join(s["name"] for s in suppliers) or "none"
                payload = await self.deps.model.search(
                    system=self.system(),
                    prompt=(
                        f"Find up to {count - len(suppliers)} suppliers in {where} who can supply these items. "
                        f"Leave out: {known}. Only list a supplier a web page shows supplies them; give that page "
                        f"as evidence_url. Contact details only as found on their own site, else empty.\n\n{wanted_items}"
                    ),
                    schema=SUPPLIERS_SCHEMA, user_key=self.user_key, spent=spent,
                )
                for found in payload.get("suppliers") or []:
                    name = _s(found.get("name"))
                    if not name or name in by_supplier:
                        continue
                    suppliers.append({
                        "name": name, "items_covered": [str(x)[:80] for x in found.get("items_covered") or []],
                        "last_quoted": None, "from": "web", "email": _s(found.get("email")) or None,
                        "phone": _s(found.get("phone")) or None, "contact_person": None,
                        "place": _s(found.get("city")) or None, "website": _s(found.get("website")) or None,
                        "source": source(f"web: {name}", _s(found.get("evidence"))[:300], _s(found.get("evidence_url")) or None),
                    })
                    web_found += 1
        elif not suppliers and not cfg.get("web"):
            notes.append("None of our past supplier quotes match these items. Turn on “Search the web” on this step to look further.")

        ctx.suppliers = suppliers
        how = f"{len(suppliers) - web_found} from our supplier quotes" + (f", {web_found} from the web" if web_found else " — no AI used")
        return Out(
            headline=f"{len(suppliers)} supplier(s): {how}",
            output={"suppliers": suppliers}, sources=[s["source"] for s in suppliers], notes=notes,
        )

    async def s_compare_quotes(self, cfg: dict[str, Any], spent: Spent) -> Out:
        ctx = self.ctx
        items = ctx.items
        if not items:
            return skipped("No items to compare — pull out the items first.")
        if ctx.history is None:
            ctx.history = await self._history(items, None)
        pick = _s(cfg.get("pick")) or "cheapest"
        margin = _dec(cfg.get("margin")) or Decimal(0)
        rows = []
        notes: list[str] = []
        for i, item in enumerate(items):
            offers = [m for m in ctx.history.get(i, []) if m["source"] == "supplier_quote" and m.get("rate")]
            if not offers:
                rows.append({"item": item_line(item), "supplier": None, "rate": None, "currency": None, "date": None, "sell": None, "source": None})
                continue
            if pick == "cheapest":
                best = min(offers, key=lambda m: Decimal(m["rate"]))
            else:
                best = max(offers, key=lambda m: m["date"] or "")
            rate = Decimal(best["rate"])
            sell = rate / (1 - margin / 100) if margin < 100 else None
            rows.append({
                "item": item_line(item), "supplier": best["supplier"], "rate": best["rate"], "currency": best["currency"],
                "date": best["date"], "sell": _money(sell), "source": source(best["label"], f"{best['ref']} · {best['date'] or 'no date'}"),
            })
        if pick == "fastest":
            notes.append("Delivery times are not on record for past offers, so the newest offer was taken.")
        currencies = {r["currency"] for r in rows if r["currency"]}
        if len(currencies) > 1:
            notes.append(f"Offers are in {', '.join(sorted(currencies))} — not converted. Check before adding them up.")
        priced = sum(1 for r in rows if r["rate"])
        ctx.offers = rows
        return Out(
            headline=f"Best past offer for {priced} of {len(items)} item(s) — no AI used",
            output={"rows": rows, "pick": pick, "margin": str(margin)},
            sources=[r["source"] for r in rows if r["source"]][:30], notes=notes,
        )

    async def s_summarise(self, cfg: dict[str, Any], spent: Spent) -> Out:
        ctx = self.ctx
        bullets: list[dict[str, Any]] = []
        add = lambda text, label: bullets.append({"text": text, "source": label})  # noqa: E731
        if ctx.task_title:
            add(f"Task: {ctx.task_title}. End user: {ctx.end_user or NOT_GIVEN}. Closes: {_iso(ctx.when()) or NOT_GIVEN}.", "task")
        if ctx.reading:
            r = ctx.reading
            add(f"{len(ctx.items)} item(s) required. Customer: {r.get('customer') or NOT_GIVEN}.", "documents")
            for item in ctx.items[:5]:
                add(item_line(item), f"document: {item.get('source_document') or 'unknown'}")
            if len(ctx.items) > 5:
                add(f"…and {len(ctx.items) - 5} more.", "documents")
            for c in (r.get("conditions") or [])[:4]:
                add(f"Condition: {c}", "documents")
            if r.get("missing"):
                add(f"Not given in the documents: {'; '.join(r['missing'][:4])}.", "documents")
        if ctx.tasks is not None:
            late = [t for t in ctx.tasks if t["days_left"] is not None and t["days_left"] < 0]
            soon = [t for t in ctx.tasks if t["days_left"] is not None and 0 <= t["days_left"] <= 7]
            add(f"{len(late)} late, {len(soon)} due within a week, {len(ctx.tasks)} in all.", "Proposals list")
            for t in (late + soon)[:8]:
                when = "late" if t["days_left"] < 0 else f"in {t['days_left']} day(s)"
                add(f"{t['title']} — {when} ({t['deadline']})", "Proposals list")
        if ctx.suppliers:
            add(f"{len(ctx.suppliers)} supplier(s) found: {', '.join(s['name'] for s in ctx.suppliers[:6])}.", "suppliers")
        if ctx.offers:
            priced = sum(1 for o in ctx.offers if o.get("rate"))
            add(f"Past prices for {priced} of {len(ctx.offers)} item(s).", "our supplier quotes")
        for a in ctx.answers[-2:]:
            add(f"You said: {a}", "your answer")
        if not bullets:
            return skipped("Nothing to summarise yet — put this step after the steps that gather something.")

        length = _s(cfg.get("length")) or "bullets"
        audience = _s(cfg.get("for"))
        # On unless switched off: the facts are the app's, the wording is better written.
        if cfg.get("ai", True) is not False:
            try:
                payload = await self.ask_json(
                    system=self.system(),
                    text=(
                        f"Write these facts up as {'one line' if length == 'line' else 'a short paragraph' if length == 'para' else 'short bullet points'}"
                        f"{f' for {audience}' if audience else ''}, the way a capable colleague would: lead with what "
                        "matters most (what is late, what closes soon, what is missing), keep every fact, add nothing "
                        "that is not below.\n\n" + "\n".join(f"[{b['source']}] {b['text']}" for b in bullets)
                    ),
                    schema=SUMMARY_SCHEMA, spent=spent, max_tokens=4000,
                )
                text = _s(payload.get("text"))
                if text:
                    return Out(headline="Summary written", output={"text": text, "bullets": bullets, "for": audience or None})
            except LLMError:
                pass  # The app's own version below is still a summary.
        if length == "line":
            text = bullets[0]["text"]
        elif length == "para":
            text = " ".join(b["text"] for b in bullets[:6])
        else:
            text = ""
        return Out(headline="Summary ready — no AI used", output={"text": text, "bullets": bullets, "for": audience or None})

    async def s_ask_model(self, cfg: dict[str, Any], spent: Spent) -> Out:
        prompt = _s(cfg.get("prompt"))
        if not prompt:
            return skipped("This step has no instruction yet.")
        shape = {"list": "a short list", "table": "a short list of rows"}.get(_s(cfg.get("shape")), "a few sentences")
        ask = f"{prompt}\n\nAnswer as {shape}. Give each point its source label. List what the material does not say under not_found."
        if cfg.get("web") and self.deps.model.web_allowed and self.deps.model.configured:
            payload = await self.deps.model.search(
                system=self.system(), prompt=f"{ask}\n\nMaterial:\n{self.material()}", schema=ANSWER_SCHEMA,
                user_key=self.user_key, spent=spent,
            )
        else:
            payload = await self.ask_json(
                system=self.system(), text=f"Material:\n{self.material()}\n\nTask: {ask}",
                schema=ANSWER_SCHEMA, spent=spent, max_tokens=8000,
            )
        points = [{"text": _s(p.get("text")), "source": _s(p.get("source")) or NOT_GIVEN} for p in payload.get("points") or [] if _s(p.get("text"))]
        answer = {"answer": _s(payload.get("answer")), "points": points, "not_found": [_s(x) for x in payload.get("not_found") or [] if _s(x)]}
        self.ctx.answers.append(answer["answer"][:500])
        return Out(headline=_first_line(answer["answer"]) or "Answered", output=answer)

    async def s_check(self, cfg: dict[str, Any], spent: Spent, results: list[dict[str, Any]], index: int) -> Out:
        condition = _s(cfg.get("if"))
        if not condition:
            return skipped("No condition set.")
        verdict = self._check_by_rule(condition)
        how = "worked out from the task — no AI used"
        if verdict is None:
            payload = await self.ask_json(
                system=self.system(),
                text=f"Material:\n{self.material()}\n\nIs this true: {condition}?\nIf the material cannot tell, passed is false and say so.",
                schema=CHECK_SCHEMA, spent=spent, max_tokens=2000,
            )
            verdict = (bool(payload.get("passed")), _s(payload.get("reason")) or NOT_GIVEN)
            how = "judged by AI"
        passed, reason = verdict
        if passed:
            return Out(headline=f"Yes: {reason} ({how})", output={"passed": True, "reason": reason})
        otherwise = _s(cfg.get("else")) or "tell"
        if otherwise == "ask":
            answer = await self.wait_for_answer(
                {"title": f"“{condition}” is not true", "message": f"{reason}. Carry on anyway?", "mode": "review"}, results, index)
            if not answer["go"]:
                raise Stop(f"Condition not met: {condition}.")
            return Out(headline=f"Not true ({reason}), but you said carry on", output={"passed": False, "reason": reason})
        if otherwise == "tell":
            await notify(
                self.session, users=[self.user.id], kind=NotificationKind.GENERAL,
                title=f"{self.agent_name} stopped: {condition} — no", body=reason, link=self.link(),
                source="agent_check", source_id=f"{self.run.id}:{index}",
            )
            self.ctx.notified = True
        raise Stop(f"Condition not met: {condition} ({reason}).")

    def _check_by_rule(self, condition: str) -> tuple[bool, str] | None:
        """Conditions the app can answer itself. None when it cannot."""
        ctx = self.ctx
        text = condition.lower()
        days = re.search(r"(more|less|fewer) than (\d+) days?", text)
        if days and re.search(r"deadline|closing|due|bcd|left|away", text):
            when = ctx.when()
            if when is None:
                return False, "the task has no closing or due date"
            left = (when - date.today()).days
            wanted = int(days.group(2))
            ok = left > wanted if days.group(1) == "more" else left < wanted
            return ok, f"{left} day(s) left until {when.isoformat()}"
        if re.search(r"\b(has|have|found|any)\b.*\bdocuments?\b|\bdocuments? (found|attached)\b", text):
            return bool(ctx.docs), f"{len(ctx.docs)} document(s) read"
        if re.search(r"\bitems?\b.*\b(found|read)\b|\b(has|have|any)\b.*\bitems?\b", text):
            return bool(ctx.items), f"{len(ctx.items)} item(s) found"
        if re.search(r"\bsuppliers?\b.*\bfound\b|\b(has|have|any)\b.*\bsuppliers?\b", text):
            return bool(ctx.suppliers), f"{len(ctx.suppliers)} supplier(s) found"
        return None

    async def s_ask_me(self, cfg: dict[str, Any], spent: Spent, results: list[dict[str, Any]], index: int) -> Out:
        question = _s(cfg.get("question")) or "Carry on?"
        mode = _s(cfg.get("mode")) or "review"
        before = next((r for r in reversed(results[:index]) if r["status"] == "done"), None)
        answer = await self.wait_for_answer({
            "title": question, "mode": mode,
            "message": f"About: {before['name']} — {before['headline']}" if before else None,
            "about_step_id": before["step_id"] if before else None,
        }, results, index)
        if not answer["go"]:
            raise Stop(f"You stopped it at: {question}")
        if answer["text"]:
            self.ctx.answers.append(answer["text"])
        notes = ["Uploading files here comes later; your typed answer was kept."] if mode == "files" else []
        return Out(headline=f"You answered: {answer['text']}" if answer["text"] else "You said carry on",
                   output={"answer": answer["text"]}, sources=[source("your answer")], notes=notes)

    async def s_get_approval(self, cfg: dict[str, Any], spent: Spent, results: list[dict[str, Any]], index: int) -> Out:
        notes = []
        if _s(cfg.get("from")) == "manager":
            notes.append("Asking your manager comes later; you approve for now.")
        answer = await self.wait_for_answer({
            "title": "Approve to carry on?", "mode": "review", "message": _s(cfg.get("note")) or None,
        }, results, index)
        if not answer["go"]:
            raise Stop("Not approved.")
        return Out(headline="Approved", output={"answer": answer["text"]}, notes=notes, sources=[source("your answer")])

    async def s_draft_email(self, cfg: dict[str, Any], spent: Spent) -> Out:
        ctx, user = self.ctx, self.user
        to = _s(cfg.get("to")) or "suppliers"
        recipients: list[dict[str, Any]] = []
        if to == "suppliers":
            if not ctx.suppliers:
                return skipped("No suppliers to write to — put a “Find suppliers” step before this one.")
            recipients = [{"name": s["name"], "email": s.get("email"), "attn": s.get("contact_person"), "covers": s.get("items_covered") or []} for s in ctx.suppliers]
        elif to == "customer":
            recipients = [{"name": ctx.end_user or "the customer", "email": None, "attn": None, "covers": []}]
        elif to == "me":
            recipients = [{"name": user.display_name, "email": user.email, "attn": None, "covers": []}]
        else:
            address = _s(cfg.get("address"))
            recipients = [{"name": address or NOT_GIVEN, "email": address or None, "attn": None, "covers": []}]

        title = ctx.task_title or "our requirement"
        subject = (_s(cfg.get("subject")) or "Request for quotation — {task title}").replace("{task title}", title)
        extra = _s(cfg.get("body"))
        reply_by = ctx.when() - timedelta(days=2) if ctx.when() else None
        drafts = []
        for r in recipients:
            items = ctx.items
            if r["covers"]:
                covered = {c.casefold() for c in r["covers"]}
                items = [it for it in ctx.items if _s(it.get("description"))[:80].casefold() in covered] or ctx.items
            lines = [f"Dear {r['attn'] or r['name'] + ' team'},", ""]
            if to == "suppliers":
                lines.append(f"We have a requirement for the following and would welcome your best offer{f' (ref: {title})' if ctx.task_title else ''}:")
                lines.append("")
                lines += [f"{n + 1}. {item_line(it)}" for n, it in enumerate(items[:60])] or ["(items to be confirmed)"]
                lines += ["", "Please include unit price and currency, lead time, offer validity, delivery terms and country of origin."]
                if reply_by and reply_by >= date.today():
                    lines.append(f"We would appreciate your offer by {reply_by.strftime('%d %b %Y')}.")
            if extra:
                lines += ["", extra]
            lines += ["", "Kind regards,", user.display_name, "Hamdaz"]
            drafts.append({"to_name": r["name"], "to_email": r["email"], "subject": subject, "body": "\n".join(lines)})

        how = "written from a template — no AI used"
        # On unless switched off: the template carries the facts, the model the tone.
        if cfg.get("ai", True) is not False and drafts:
            polished_count = 0
            for draft in drafts[:10]:
                try:
                    payload = await self.ask_json(
                        system=self.system(),
                        text=(
                            "Rewrite this email so it reads naturally, politely and professionally, as Hamdaz "
                            "would write to a supplier or customer in the UAE. Keep every item, part number, "
                            "quantity, date and name exactly as given; add no facts and no prices. Return the "
                            "full email body as text.\n\n" + draft["body"]
                        ),
                        schema=SUMMARY_SCHEMA, spent=spent, max_tokens=4000,
                    )
                except LLMError:
                    break
                polished = _s(payload.get("text"))
                if polished:
                    draft["body"] = polished
                    polished_count += 1
            if polished_count:
                how = "wording by AI" if polished_count == len(drafts) else f"{polished_count} worded by AI"
        missing = sum(1 for d in drafts if not d["to_email"])
        for n, draft in enumerate(drafts):
            draft["n"] = n
        ctx.drafts = drafts
        notes = ["Drafts only — nothing was sent. Copy one, or open it in your mail, and send it yourself."]
        if missing:
            notes.append(f"{missing} recipient(s) have no email address on record — {NOT_GIVEN}.")
        return Out(
            headline=f"{len(drafts)} draft email(s), {how}",
            output={"drafts": drafts}, notes=notes,
            sources=[s["source"] for s in ctx.suppliers] if to == "suppliers" else [],
        )

    async def s_send_emails(self, cfg: dict[str, Any], spent: Spent, results: list[dict[str, Any]], index: int) -> Out:
        """Ask the owner which drafts to send, then send those from their own mailbox.

        Nothing goes without the yes: the drafts are shown in full in the app,
        where the owner edits what needs it, ticks the ones to send and
        confirms (Teams only links there). Only those are sent — as the owner,
        through Graph, so they sit in the owner's Sent Items and the replies
        come back to them.
        """
        ctx, user = self.ctx, self.user
        drafts = ctx.drafts
        if not drafts:
            return skipped("No drafts to send — put a “Draft an email” step before this one.")
        sendable = [d["n"] for d in drafts if _EMAIL.match(_s(d.get("to_email")))]
        if self.deps.mailer is None or not getattr(user, "entra_object_id", None):
            return skipped("Your mailbox cannot be sent from here (no Microsoft account is linked), so nothing was sent.")
        # Asked even when no draft has an address: one can be added in the app.
        title = (
            f"Review {len(sendable)} RFQ email(s) before they go" if sendable
            else f"{len(drafts)} RFQ draft(s) ready — none has an email address yet"
        )
        message = (
            f"Nothing has gone yet. Read each email, edit what needs it and tick the ones to send — they go "
            f"from {user.email} only after you confirm."
            if sendable else "Add an address to any of them, read and tick it, and confirm — then it goes from your mailbox."
        )
        test_to = test_mail_to(self.deps.settings)
        if test_to:
            message = f"TEST MODE — every email goes to {test_to} only; no supplier receives anything. " + message
        answer = await self.wait_for_answer({
            "title": title,
            "message": message,
            "mode": "send",
            "about_step_id": ctx.drafts_step_id,
            "test_to": test_to or None,
            "drafts": [
                {"n": d["n"], "to_name": d.get("to_name"), "to_email": d.get("to_email"),
                 "subject": d.get("subject"), "body": str(d.get("body") or "")[:20_000],
                 "sendable": d["n"] in sendable}
                for d in drafts
            ],
        }, results, index, give_up_after=timedelta(days=3), timeout_message="No answer came within 3 days, so nothing was sent.")
        out = await deliver(self.deps, user, drafts, answer)
        from app.agents import rfqs

        rfqs.record(self.session, self.run, out.output.get("sent") or [])
        return out

    async def s_draft_quote_request(self, cfg: dict[str, Any], spent: Spent) -> Out:
        ctx = self.ctx
        items = ctx.items
        if not items:
            return skipped("No items to quote — pull out the items first.")
        if not ctx.offers:
            await self.s_compare_quotes({"pick": "cheapest", "margin": 0}, spent)
        currency = _s(cfg.get("currency")) or "AED"
        margin = _dec(cfg.get("margin"))
        margin = Decimal(15) if margin is None else margin
        lines = []
        other_currency = False
        for item, offer in zip(items, ctx.offers, strict=False):
            cost = _dec(offer.get("rate"))
            sell = cost / (1 - margin / 100) if cost is not None and margin < 100 else None
            if offer.get("currency") and offer["currency"] != currency:
                other_currency = True
            lines.append({
                "description": item.get("description"), "part_number": item.get("part_number"),
                "quantity": item.get("quantity"), "unit": item.get("unit"),
                "cost": _money(cost), "cost_currency": offer.get("currency"), "sell": _money(sell),
                "supplier": offer.get("supplier"), "source": offer.get("source"),
            })
        notes = ["A draft — it is not in Quote requests. Raise it there when you are happy with it."]
        if other_currency:
            notes.append(f"Some costs are not in {currency}; they were not converted.")
        priced = sum(1 for l in lines if l["cost"])
        return Out(
            headline=f"Draft quote: {len(lines)} line(s), {priced} priced from past offers at {margin}% margin — no AI used",
            output={"currency": currency, "margin": str(margin), "lines": lines}, notes=notes,
            sources=[l["source"] for l in lines if l["source"]][:30],
        )

    async def s_make_document(self, cfg: dict[str, Any], spent: Spent) -> Out:
        return skipped("Making a PDF or Excel comes next. Every result on this page can be copied meanwhile.")

    async def s_notify_me(self, cfg: dict[str, Any], spent: Spent) -> Out:
        ctx = self.ctx
        message = _s(cfg.get("message")) or ("; ".join(ctx.headlines[-4:]) or "Nothing to report yet.")
        title = f"{self.agent_name}{f' — {ctx.task_title}' if ctx.task_title else ''}"
        await notify(
            self.session, users=[self.user.id], kind=NotificationKind.GENERAL, title=title, body=message[:1000],
            link=self.link(), source="agent_notify", source_id=f"{self.run.id}:{len(ctx.headlines)}",
        )
        ctx.notified = True
        via = _s(cfg.get("via")) or "app"
        where = "in the app"
        if via in ("teams", "both") and await tell_in_teams(
            self.deps, self.user, self.run, agent=self.agent_name, title=title, lead=message,
            points=[], button="Open in the app", face=self.face,
        ):
            where = "in the app and in Teams"
        notes = ["Email to yourself comes later; this went to the bell in the app."] if via == "email" else []
        return Out(headline=f"You were told {where}", output={"title": title, "message": message}, notes=notes)

    async def s_wait_replies(self, cfg: dict[str, Any], spent: Spent) -> Out:
        return skipped("Waiting for replies comes with the RFQ mail step. A run started by hand carries on.")

    async def s_wait_status(self, cfg: dict[str, Any], spent: Spent) -> Out:
        return skipped("Waiting on a status is not built yet. A run started by hand carries on.")

    async def s_pause(self, cfg: dict[str, Any], spent: Spent) -> Out:
        return skipped("Pauses are not built yet. A run started by hand carries on.")


# ── schemas for the model ──────────────────────────────────────────────

SUPPLIERS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "suppliers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "website": {"type": "string", "description": "Homepage, or empty."},
                    "email": {"type": "string", "description": "As shown on their own site, or empty."},
                    "phone": {"type": "string", "description": "As shown on their own site, or empty."},
                    "city": {"type": "string", "description": "City or emirate, or empty."},
                    "items_covered": {"type": "array", "items": {"type": "string"}},
                    "evidence_url": {"type": "string", "description": "The page that shows they supply these."},
                    "evidence": {"type": "string", "description": "What that page says, in one line."},
                },
                "required": ["name", "website", "email", "phone", "city", "items_covered", "evidence_url", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["suppliers"],
    "additionalProperties": False,
}

ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "The answer, briefly."},
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source": {"type": "string", "description": "The material's label for where this came from, or a web page URL."},
                },
                "required": ["text", "source"],
                "additionalProperties": False,
            },
        },
        "not_found": {"type": "array", "items": {"type": "string"}, "description": "What the material does not say."},
    },
    "required": ["answer", "points", "not_found"],
    "additionalProperties": False,
}

SUMMARY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

CHECK_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reason": {"type": "string", "description": "Why, in one line, with the source label."},
    },
    "required": ["passed", "reason"],
    "additionalProperties": False,
}


# ── small things ───────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def _first_line(text: str) -> str:
    line = text.strip().split("\n", 1)[0]
    return line[:200] + ("…" if len(line) > 200 else "")


def _item_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[_s(item.get("source_document")) or "unknown"] += 1
    return [source(f"document: {name}", f"{n} item(s)") for name, n in counts.items()]


# ── starting, and runs a restart cut off ───────────────────────────────


def start(deps: Deps, run_id: uuid.UUID, uploads: list[tuple[str, bytes, str | None]], *, resume: bool = False) -> Runner:
    if run_id in RUNNING:
        return RUNNING[run_id]
    runner = Runner(deps, run_id, uploads, resume=resume)
    task = asyncio.create_task(runner.go(), name=f"agent-run-{run_id}")
    runner.task = task
    RUNNING[run_id] = runner
    task.add_done_callback(lambda _t: RUNNING.pop(run_id, None))
    return runner


def deps_for(state: Any, settings: Settings, factory: async_sessionmaker[AsyncSession]) -> Deps:
    """What a run needs, from the application's state."""
    return Deps(
        settings=settings,
        factory=factory,
        sharepoint=state.sharepoint,
        drive=state.quote_drive,
        model=state.agent_model,
        free=state.text_model,
        mailer=getattr(state, "agent_mailer", None),
        http=state.http,
        state=state,
    )


def is_cut_off(run: AgentRun) -> bool:
    """A live run no process is working on: not here, and its heartbeat has stopped."""
    if run.status not in ("running", "waiting") or run.id in RUNNING:
        return False
    beat = run.heartbeat_at or run.started_at or run.created_at
    return beat is None or datetime.now(UTC) - beat > CUT_OFF_AFTER


def resumable(run: AgentRun) -> bool:
    """Cut off while waiting on a question it can still be answered on."""
    return run.status == "waiting" and (run.question or {}).get("mode") in RESUMABLE


def settle_orphan(run: AgentRun) -> bool:
    """A run nobody is running, left marked as live: say it was cut off.

    Not one waiting for documents or for a yes to send — those are picked up
    again (``answer_cut_off`` and the worker).
    """
    if not is_cut_off(run) or resumable(run):
        return False
    run.status = "failed"
    run.error = "The run was cut off (the server restarted). Run it again."
    run.summary = run.summary or run.error
    run.question = None
    run.finished_at = datetime.now(UTC)
    return True


def record_answer(run: AgentRun, answer: dict[str, Any]) -> None:
    """Write the owner's answer onto the run, where any process — or a restart — finds it."""
    run.question = {**(run.question or {}), "answer": answer, "answered_at": _now()}


async def answer_cut_off(deps: Deps, run_id: uuid.UUID) -> None:
    """A cut-off run has its answer, or has been found by the worker.

    Waiting for documents: it starts again from the top, finds the question
    already asked, and carries on. Waiting to send, with the answer in: the
    ticked drafts are sent from the saved results and the run finishes.
    """
    async with deps.factory() as session:
        run = await session.get(AgentRun, run_id)
        if run is None or not is_cut_off(run) or not resumable(run):
            return
        question = run.question or {}
        if question.get("mode") == "docs":
            run.heartbeat_at = datetime.now(UTC)  # claimed, so nobody else starts it too
            await session.commit()
            start(deps, run.id, [], resume=True)
            return
        answer = question.get("answer")
        user = await session.get(User, run.owner_id)
        results = [dict(r) for r in run.results or []]
        index = next((i for i, r in enumerate(results) if r["step_id"] == question.get("step_id")), None)
        if not isinstance(answer, dict) or user is None or index is None:
            return
        drafted = next((r for r in results if r["step_id"] == question.get("about_step_id")), None) or next(
            (r for r in reversed(results[:index]) if r["kind"] == "draft_email"), None
        )
        drafts = list(((drafted or {}).get("output") or {}).get("drafts") or [])
        for n, draft in enumerate(drafts):
            draft.setdefault("n", n)
        run.status, run.heartbeat_at = "running", datetime.now(UTC)
        await session.commit()

        out = await deliver(deps, user, drafts, answer)
        from app.agents import rfqs

        rfqs.record(session, run, out.output.get("sent") or [])
        results[index].update(status=out.status, headline=out.headline, output=out.output, sources=out.sources,
                              notes=out.notes, finished_at=_now())
        for r in results[index + 1:]:
            if r["status"] in ("pending", "running", "waiting"):
                r.update(status="stopped", headline="Not run — a restart cut the run off before this step")
        name = _s((run.plan or {}).get("name")) or "Your agent"
        where = f" on “{run.task_title}”" if run.task_title else ""
        run.results, run.question, run.status = results, None, "done"
        run.summary = f"{name} finished{where}. {out.headline}."
        run.finished_at = datetime.now(UTC)
        await notify(session, users=[user.id], kind=NotificationKind.GENERAL, title=f"{name}: finished{where}",
                     body=run.summary[:1000], link=run_link(run), source="agent_run", source_id=str(run.id))
        await session.commit()
        face = face_url(deps.settings, await session.get(UserAgent, run.agent_id))
        await tell_in_teams(deps, user, run, agent=name, title=f"{name} finished{where}", lead=out.headline,
                            points=_sent_lines(out.output), button="See the results", face=face)


# ── sending, as the owner ──────────────────────────────────────────────


def test_mail_to(settings: Any) -> str:
    """The one inbox every agent email goes to while testing (AGENT_MAIL_REDIRECT); empty when live."""
    return _s(getattr(settings, "agent_mail_redirect", ""))


def sends_for_real(settings: Any) -> bool:
    """Whether agent emails may reach the people they are addressed to.

    All three, or no supplier hears from an agent: the live server, the
    AGENT_MAIL_LIVE switch turned on, and no test redirect.
    """
    return (
        getattr(settings, "environment", "local") == "production"
        and getattr(settings, "agent_mail_live", False) is True
        and not test_mail_to(settings)
    )


async def deliver(deps: Deps, user: User, drafts: list[dict[str, Any]], answer: dict[str, Any]) -> Out:
    """Send the drafts the owner ticked, from their own mailbox. Each one's fate is recorded."""
    by = "in Teams" if answer.get("by") == "teams" else "in the app"
    said = [source("your answer", by)]
    if not answer.get("go"):
        return Out(headline="You said don't send — nothing was sent", output={"sent": [], "declined": True}, sources=said)
    if deps.mailer is None or not getattr(user, "entra_object_id", None):
        return Out(headline="Your mailbox could not be sent from, so nothing was sent", status="failed", sources=said)
    by_n = {int(d.get("n", i)): d for i, d in enumerate(drafts)}
    picks = answer.get("picks")
    if not isinstance(picks, list) or not answer.get("reviewed"):
        # The failsafe: only emails the owner picked one by one, after reviewing them in the app.
        return Out(headline="Nothing was sent — the emails were not reviewed and confirmed in the app",
                   output={"sent": [], "declined": True}, sources=said)
    edits = answer.get("edits") if isinstance(answer.get("edits"), dict) else {}
    chosen = sorted({int(n) for n in picks if str(n).lstrip("-").isdigit() and int(n) in by_n})
    # AGENT_MAIL_REDIRECT: a test sends everything to one inbox, never to a supplier.
    redirect = test_mail_to(getattr(deps, "settings", None))
    if not redirect and not sends_for_real(getattr(deps, "settings", None)):
        # Only the live server with AGENT_MAIL_LIVE writes to suppliers. Otherwise no redirect means no mail.
        return Out(
            headline="Not sent: sending to suppliers is off (AGENT_MAIL_LIVE), and no test address is set (AGENT_MAIL_REDIRECT)",
            status="failed", sources=said,
        )
    sent: list[dict[str, Any]] = []
    for n in chosen:
        draft, edit = by_n[n], edits.get(str(n)) or {}
        to = _s(edit.get("to_email")) or _s(draft.get("to_email"))
        subject = (_s(edit.get("subject")) or _s(draft.get("subject")))[:250]
        body = str(edit.get("body") or draft.get("body") or "")
        entry = {"n": n, "to_name": draft.get("to_name"), "to_email": to, "subject": subject, "status": "sent", "error": None}
        if not _EMAIL.match(to):
            entry.update(status="skipped", error="no valid email address")
        else:
            really, title = to, subject
            if redirect:
                really, title = redirect, f"[TEST — for {to}] {subject}"[:250]
                entry["redirected_to"] = redirect
            entry["sent_subject"] = title
            try:
                await deps.mailer.send(sender=user.entra_object_id, recipients=[really], subject=title, html=_mail_html(body))
            except Exception as exc:  # noqa: BLE001 — one refused mail must not stop the rest
                logger.warning("agent mail to %s failed", to, exc_info=True)
                entry.update(status="failed", error=str(exc)[:300])
        sent.append(entry)
    left_out = [{"to_name": d.get("to_name"), "to_email": d.get("to_email")} for n, d in by_n.items() if n not in chosen]
    ok = sum(1 for s in sent if s["status"] == "sent")
    failed = sum(1 for s in sent if s["status"] == "failed")
    headline = f"Sent {ok} email(s) from {user.email}" if ok else "Nothing was sent"
    if failed:
        headline += f"; {failed} could not be sent"
    notes = []
    if redirect and ok:
        headline += f" — test mode, all to {redirect}"
        notes.append(f"Test mode (AGENT_MAIL_REDIRECT): every email went to {redirect}, not to the suppliers.")
    if answer.get("text"):
        notes.append(f"You said: {answer['text']}")
    if left_out:
        notes.append("Not sent: " + ", ".join(_s(d["to_name"]) or _s(d["to_email"]) for d in left_out[:10]) + ".")
    if ok:
        notes.append("They are in your Sent Items. I'm watching your inbox for the replies and will tell you in Teams.")
    return Out(
        headline=headline, output={"sent": sent, "left_out": left_out, "from": user.email},
        sources=said, notes=notes, status="done" if ok or not failed else "failed",
    )


def _mail_html(body: str) -> str:
    text = _html(body).replace("\r\n", "\n").replace("\n", "<br>")
    return f"<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#111;line-height:1.5'>{text}</div>"


def _sent_lines(output: dict[str, Any]) -> list[str]:
    return [
        f"{s.get('to_name')} — {s.get('to_email')}" + ("" if s.get("status") == "sent" else f" ({s.get('error') or s.get('status')})")
        for s in output.get("sent") or []
    ]


# ── talking in Teams ───────────────────────────────────────────────────


def run_link(run: AgentRun) -> str:
    return f"/agents/{run.agent_id}?run={run.id}"


async def post_card(
    deps: Deps, user: User, run: AgentRun, *, step_id: str, title: str, agent: str, task: str | None,
    card: dict[str, Any],
) -> bool:
    """One card to the owner's Teams chat through the ask flow (AGENT_TEAMS_ASK_URL). True if it went."""
    url = deps.settings.agent_teams_ask_url
    if not url or deps.http is None or not user.email:
        return False
    base = (deps.settings.public_base_url or "").rstrip("/")
    payload = {
        "recipient": user.email,
        "run_id": str(run.id),
        "step_id": step_id,
        "token": answer_token(deps.settings.session_secret, run.id, step_id),
        "callback_url": f"{base}{deps.settings.api_prefix}/agents/teams/answer" if base else "",
        "agent": agent,
        "task": task or "",
        "title": title,
        "link": f"{deps.settings.frontend_url.rstrip('/')}{run_link(run)}",
        "card": card,
    }
    try:
        response = await deps.http.post(url, json=payload, timeout=20)
        if response.status_code >= 400:
            logger.warning("agent run %s: the Teams flow refused the card (%s)", run.id, response.status_code)
            return False
    except Exception:  # noqa: BLE001 — Teams is a copy; the app has it too
        logger.warning("agent run %s: the Teams flow could not be reached", run.id, exc_info=True)
        return False
    return True


async def tell_in_teams(
    deps: Deps, user: User, run: AgentRun, *, agent: str, title: str, lead: str, points: list[str], button: str,
    face: str | None = None,
) -> bool:
    """Tell the owner something in Teams — no question. True if it went.

    The plain webhook (AGENT_TEAMS_WEBHOOK_URL) when there is one; otherwise
    the ask flow, with a card whose only button is "Got it".
    """
    link = f"{deps.settings.frontend_url.rstrip('/')}{run_link(run)}"
    if deps.http is not None and deps.settings.agent_teams_webhook_url:
        return await send_to_teams(
            deps.http, deps.settings.agent_teams_webhook_url, title=title[:200],
            body="\n\n".join([lead, *[f"• {p}" for p in points[:10]]])[:1800], link=link, recipient=user.email,
        )
    return await post_card(
        deps, user, run, step_id=f"info-{uuid.uuid4().hex[:12]}", title=title, agent=agent, task=run.task_title,
        card=info_card(title=title, lead=lead, points=points, button=button, link=link, face=face, agent=agent),
    )


def parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _html(text: str) -> str:
    import html

    return html.escape(text or "")


def _example(schema: dict[str, Any]) -> Any:
    """A made-up value in the schema's shape, to show a model what to answer with."""
    kind = schema.get("type")
    if kind == "object":
        return {k: _example(v) for k, v in (schema.get("properties") or {}).items()}
    if kind == "array":
        return [_example(schema.get("items") or {"type": "string"})]
    if kind == "boolean":
        return True
    if kind in ("number", "integer"):
        return 1
    if "enum" in schema:
        return schema["enum"][0]
    return "..."


def _fits(payload: Any, schema: dict[str, Any]) -> bool:
    """Whether an answer has the schema's required keys, each of the right kind."""
    if schema.get("type") != "object" or not isinstance(payload, dict):
        return False
    kinds = {"string": str, "boolean": bool, "array": list, "object": dict, "number": (int, float), "integer": int}
    for key in schema.get("required") or []:
        if key not in payload:
            return False
        want = kinds.get((schema.get("properties") or {}).get(key, {}).get("type", ""))
        if want is not None and not isinstance(payload[key], want):
            return False
    return True


# ── asking in Teams ────────────────────────────────────────────────────


def answer_token(secret: str, run_id: uuid.UUID | str, step_id: str) -> str:
    """The signature on one question: this run, this step, nothing else."""
    import hashlib
    import hmac

    message = f"agent-answer:{run_id}:{step_id}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _preview(about: dict[str, Any] | None) -> list[str]:
    """A few lines of what the question is about, for the card."""
    if not about:
        return []
    out = about.get("output") or {}
    lines: list[str] = []
    for s in out.get("suppliers") or []:
        lines.append(f"{s.get('name')} — {s.get('email') or 'no email on record'}")
    for item in out.get("items") or []:
        if isinstance(item, dict) and item.get("description"):
            lines.append(item_line(item))
    for d in out.get("drafts") or []:
        lines.append(f"To {d.get('to_name')}: {d.get('subject')}")
    for t in out.get("tasks") or []:
        lines.append(f"{t.get('title')} — {t.get('deadline') or 'no date'}")
    for row in out.get("rows") or []:
        if row.get("rate"):
            lines.append(f"{row.get('item')}: {row.get('rate')} {row.get('currency') or ''} ({row.get('supplier')})")
    return lines[:10]


def question_card(
    *, agent: str, task: str | None, question: dict[str, Any], about: dict[str, Any] | None, link: str,
    face: str | None = None,
) -> dict[str, Any]:
    """The Adaptive Card the flow posts: the question, what it is about, a note box, two buttons.

    "Post adaptive card and wait for a response" hands back ``data.go`` (from
    the button pressed) and ``data.note`` (from the box).
    """
    body: list[dict[str, Any]] = _header(f"{agent} needs your answer", task, face)
    body.append({"type": "TextBlock", "text": str(question.get("title") or "Carry on?"), "weight": "Bolder", "wrap": True})
    if question.get("message"):
        body.append({"type": "TextBlock", "text": str(question["message"]), "wrap": True, "isSubtle": True})
    lines = _preview(about)
    if about and lines:
        body.append({"type": "TextBlock", "text": str(about.get("name") or ""), "weight": "Bolder", "spacing": "Medium", "size": "Small"})
        body.append({"type": "TextBlock", "text": "\n".join(f"- {line}" for line in lines), "wrap": True, "size": "Small"})
    body.append({
        "type": "Input.Text", "id": "note", "isMultiline": True,
        "placeholder": "Anything to add (optional) — e.g. leave out supplier X",
    })
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": [
            {"type": "Action.Submit", "title": "Carry on", "style": "positive", "data": {"go": True}},
            {"type": "Action.Submit", "title": "Stop here", "style": "destructive", "data": {"go": False}},
            {"type": "Action.OpenUrl", "title": "Open in the app", "url": link},
        ],
    }


def _card(body: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": actions,
    }


def _text(text: str, **style: Any) -> dict[str, Any]:
    return {"type": "TextBlock", "text": text, "wrap": True, **style}


def face_url(settings: Any, agent: UserAgent | None) -> str | None:
    """Where Teams can fetch the agent's picture: the API's public face route, once a picture is saved."""
    base = _s(getattr(settings, "public_base_url", "")).rstrip("/")
    if agent is None or not getattr(agent, "face_key", None) or not base.startswith("https://"):
        return None
    return f"{base}{settings.api_prefix}/agents/{agent.id}/face.png?v={agent.face_key}"


def _header(title: str, task: str | None, face: str | None) -> list[dict[str, Any]]:
    """The card's top: the agent's face beside the title and the task, when there is a face."""
    texts = [_text(title, weight="Bolder", size="Medium")]
    if task:
        texts.append(_text(task, isSubtle=True, spacing="None"))
    if not face:
        return texts
    return [{
        "type": "ColumnSet",
        "columns": [
            {"type": "Column", "width": "auto", "verticalContentAlignment": "Center",
             "items": [{"type": "Image", "url": face, "width": "56px", "height": "56px", "altText": "agent"}]},
            {"type": "Column", "width": "stretch", "verticalContentAlignment": "Center", "items": texts},
        ],
    }]


def docs_card(
    *, agent: str, task: str | None, closing: date | None, question: dict[str, Any], link: str,
    face: str | None = None,
) -> dict[str, Any]:
    """"Can you share the documents?" — a link to the task's folder, and "They're in" / "Not needed"."""
    body = _header(f"{agent} here — a new task for you", task, face)
    if closing:
        body.append(_text(f"Closes {closing.strftime('%d %b %Y')}", isSubtle=True, spacing="Small", size="Small"))
    body.append(_text(str(question.get("title") or "Can you share the documents?"), weight="Bolder", spacing="Medium"))
    if question.get("message"):
        body.append(_text(str(question["message"])))
    body.append({"type": "Input.Text", "id": "note", "isMultiline": True,
                 "placeholder": "Anything I should know? (optional) — e.g. only items 1 to 5"})
    actions: list[dict[str, Any]] = []
    if question.get("folder_url"):
        actions.append({"type": "Action.OpenUrl", "title": "Open the task folder", "url": question["folder_url"]})
    actions += [
        {"type": "Action.Submit", "title": "They're in — start", "style": "positive", "data": {"go": True}},
        {"type": "Action.Submit", "title": "Not needed", "style": "destructive", "data": {"go": False}},
        {"type": "Action.OpenUrl", "title": "Open in the app", "url": link},
    ]
    return _card(body, actions)


#: Emails whose full text goes on the card; the rest are read in the app. Keeps the card under Teams' size limit.
_CARD_BODIES = 8


def send_card(
    *, agent: str, task: str | None, question: dict[str, Any], link: str, face: str | None = None
) -> dict[str, Any]:
    """The drafts to read, and a way to the app to review, edit and send them.

    Nothing can be sent from this card. Supplier mail goes only after its
    owner has read each email in full in the app, changed what needs it and
    confirmed the list of who receives what (2026-10-16). Here they can only
    look, or say don't send.
    """
    body = _header(f"{agent}: the RFQs are ready for you to review", task, face)
    test_to = question.get("test_to")
    if test_to:
        body.append({
            "type": "Container", "style": "warning", "spacing": "Medium",
            "items": [_text(f"TEST MODE — every email goes to {test_to} only. No supplier receives anything.",
                            weight="Bolder", size="Small")],
        })
    body.append(_text(str(question.get("title") or "Review these before they go"), weight="Bolder", spacing="Medium"))
    body.append(_text("Nothing has been sent. Open the app to read each email, edit it if needed, and send.",
                      isSubtle=True))
    for count, d in enumerate(question.get("drafts") or []):
        who = d.get("to_name") or d.get("to_email") or "supplier"
        where = d.get("to_email") if d.get("sendable") else "no email address on record yet"
        body.append(_text(f"{who} — {where}", size="Small", spacing="Medium"))
        if d.get("body") and count < _CARD_BODIES:
            box = f"mail_{d['n']}"
            body.append({"type": "ActionSet", "spacing": "None", "actions": [
                {"type": "Action.ToggleVisibility", "title": "Read the email", "targetElements": [box]},
            ]})
            body.append({
                "type": "Container", "id": box, "isVisible": False, "style": "emphasis",
                "items": [
                    _text(f"Subject: {d.get('subject') or ''}", weight="Bolder", size="Small"),
                    _text(str(d["body"])[:1500], size="Small"),
                ],
            })
    return _card(body, [
        {"type": "Action.OpenUrl", "title": "Review, edit and send in the app", "url": link},
        {"type": "Action.Submit", "title": "Don't send", "style": "destructive", "data": {"go": False}},
    ])


def info_card(
    *, title: str, lead: str, points: list[str], button: str, link: str, face: str | None = None, agent: str = "",
) -> dict[str, Any]:
    """Something to tell the owner, with nothing to decide."""
    body = _header(title, None, face) + [_text(lead)]
    if points:
        body.append(_text("\n".join(f"- {p}" for p in points[:10]), size="Small", spacing="Medium"))
    return _card(body, [
        {"type": "Action.OpenUrl", "title": button, "url": link},
        {"type": "Action.Submit", "title": "Got it", "data": {"go": True}},
    ])
