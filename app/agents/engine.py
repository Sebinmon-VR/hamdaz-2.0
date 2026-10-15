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

**Nothing leaves the app.** The task's documents are read, not written; the
Proposals list and Zoho are not touched; emails are drafts the owner sends
themselves. A step whose block would act on the outside world drafts instead.

A run lives in this process (``RUNNING``) while it works or waits for its
owner's answer; its row is written after every step, so the page can follow
it. A server restart ends it, and the page says so.
"""

from __future__ import annotations

import asyncio
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
from app.models.agent import AgentRun
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
    "ask_me": ("Ask me", "ask"),
    "get_approval": ("Get approval", "ask"),
    "draft_email": ("Draft an email", "act"),
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
            desc_col = _column(header, _DESC_HEAD, avoid={qty_col})
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
    seen: set[tuple[str, str]] = set()
    out = []
    for item in items:
        key = (_s(item.get("description")).casefold(), _s(item.get("part_number")).casefold())
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


SYSTEM: Final = """\
You are {agent}, an assistant working for {person} at Hamdaz, a trading and \
supply company in the UAE.

Use ONLY the material you are given. Every fact you state must carry the \
label of where it came from, exactly as the material labels it (for example \
[document: RFQ.pdf] or [task]). If the material does not say something, write \
"not given" — never guess, and never fill in from general knowledge. Be brief.
{instructions}"""


class Runner:
    def __init__(self, deps: Deps, run_id: uuid.UUID, uploads: list[tuple[str, bytes, str | None]]) -> None:
        self.deps = deps
        self.run_id = run_id
        self.uploads = uploads
        self.ctx = Context()
        self.answer: dict[str, Any] | None = None
        self.answered = asyncio.Event()
        self.stopping = False
        self.session: AsyncSession
        self.run: AgentRun
        self.user: User
        self.agent_name = "Your agent"
        self.instructions = ""
        self.task: asyncio.Task[None] | None = None

    # ── control from the routes ──

    def give_answer(self, go: bool, text: str) -> None:
        self.answer = {"go": go, "text": text.strip()[:4000]}
        self.answered.set()

    def stop(self) -> None:
        self.stopping = True
        self.answer = {"go": False, "text": ""}
        self.answered.set()

    # ── the run ──

    async def go(self) -> None:
        async with self.deps.factory() as session:
            run = await session.get(AgentRun, self.run_id)
            user = await session.get(User, run.owner_id) if run else None
            if run is None or user is None:
                return
            self.session, self.run, self.user = session, run, user
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
        run.status, run.started_at, run.model = "running", datetime.now(UTC), self.deps.model.model
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
            run.question = None
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
        # Every run ends somewhere its owner will see it.
        if not ctx.notified:
            await notify(
                self.session, users=[self.user.id], kind=NotificationKind.GENERAL,
                title=f"{self.agent_name}: {'stopped' if stopped else 'finished'}{where}",
                body=run.summary[:1000], link=self.link(), source="agent_run", source_id=str(run.id),
            )
        await self.session.commit()
        # And in their inbox, for when they are not in the app.
        await self.email_owner(
            subject=f"{self.agent_name} {'stopped' if stopped else 'finished'}{where}",
            lead=run.summary or "",
            points=[f"{r['name']}: {r['headline']}" for r in run.results if r.get("headline") and r["status"] in ("done", "failed")],
            button="See the results",
        )

    def link(self) -> str:
        return f"/agents/{self.run.agent_id}?run={self.run.id}"

    async def email_owner(self, *, subject: str, lead: str, points: list[str], button: str) -> None:
        """Tell the owner outside the app: a Teams card when a webhook is set, and a mail.

        Never fails the run: both are copies of a notification already in the bell.
        """
        deps = self.deps
        url = f"{deps.settings.frontend_url.rstrip('/')}{self.link()}"
        if deps.http is not None and deps.settings.agent_teams_webhook_url:
            await send_to_teams(
                deps.http, deps.settings.agent_teams_webhook_url, title=subject[:200],
                body="\n\n".join([lead, *[f"• {p}" for p in points[:8]]])[:1800], link=url,
                recipient=self.user.email,
            )
        if deps.mailer is None or not deps.settings.agent_email_notify:
            return
        if not self.user.email or not getattr(self.user, "entra_object_id", None):
            return
        items = "".join(f"<li style='margin:4px 0'>{_html(p)}</li>" for p in points[:12])
        html = (
            "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#111;max-width:620px'>"
            f"<p style='margin:0 0 12px'>{_html(lead)}</p>"
            + (f"<ul style='padding-left:18px;margin:0 0 16px;color:#333'>{items}</ul>" if items else "")
            + f"<p><a href='{url}' style='display:inline-block;background:#111;color:#fff;padding:9px 16px;"
            f"border-radius:10px;text-decoration:none'>{_html(button)}</a></p>"
            "<p style='color:#888;font-size:12px'>From your agent in the Hamdaz app. Drafts are never sent "
            "for you — you send them yourself.</p></div>"
        )
        try:
            await deps.mailer.send(sender=self.user.entra_object_id, recipients=[self.user.email], subject=subject[:200], html=html)
        except Exception:  # noqa: BLE001 — a mail that fails must not fail the run
            logger.warning("agent run %s: the owner could not be mailed", self.run.id, exc_info=True)

    async def ask_in_teams(self, question: dict[str, Any], results: list[dict[str, Any]], index: int) -> bool:
        """Ask the owner in Teams too, through their Power Automate flow. True if it went.

        The flow posts ``card`` to ``recipient`` with "Post adaptive card and
        wait for a response", then POSTs the answer to ``callback_url`` with
        ``run_id``, ``step_id`` and ``token`` handed back unchanged. The token
        is this run's and this question's alone (see ``answer_token``), so the
        callback cannot answer anything else. Never fails the run.
        """
        deps = self.deps
        url = deps.settings.agent_teams_ask_url
        if not url or deps.http is None or not self.user.email:
            return False
        step_id = results[index]["step_id"]
        about = next((r for r in reversed(results[:index]) if r["status"] == "done"), None)
        base = (deps.settings.public_base_url or "").rstrip("/")
        payload = {
            "recipient": self.user.email,
            "run_id": str(self.run.id),
            "step_id": step_id,
            "token": answer_token(deps.settings.session_secret, self.run.id, step_id),
            "callback_url": f"{base}{deps.settings.api_prefix}/agents/teams/answer" if base else "",
            "agent": self.agent_name,
            "task": self.ctx.task_title or "",
            "title": str(question.get("title") or "Carry on?"),
            "link": f"{deps.settings.frontend_url.rstrip('/')}{self.link()}",
            "card": question_card(
                agent=self.agent_name, task=self.ctx.task_title, question=question, about=about,
                link=f"{deps.settings.frontend_url.rstrip('/')}{self.link()}",
            ),
        }
        try:
            response = await deps.http.post(url, json=payload, timeout=20)
            if response.status_code >= 400:
                logger.warning("agent run %s: the Teams flow refused the question (%s)", self.run.id, response.status_code)
                return False
        except Exception:  # noqa: BLE001 — Teams is a copy; the app still waits for the answer
            logger.warning("agent run %s: the Teams flow could not be reached", self.run.id, exc_info=True)
            return False
        return True

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

    async def wait_for_answer(self, question: dict[str, Any], results: list[dict[str, Any]], index: int) -> dict[str, Any]:
        if self.stopping:
            raise Stop("Stopped by you.")
        self.answer = None
        self.answered.clear()
        results[index]["status"] = "waiting"
        self.run.status = "waiting"
        self.run.question = {**question, "step_id": results[index]["step_id"], "asked_at": _now()}
        self._write(results)
        await notify(
            self.session, users=[self.user.id], kind=NotificationKind.GENERAL,
            title=f"{self.agent_name} needs your answer", body=str(question.get("title") or "")[:500],
            link=self.link(), source="agent_wait", source_id=f"{self.run.id}:{index}",
        )
        await self.session.commit()
        await self.email_owner(
            subject=f"{self.agent_name} needs your answer{f' — {self.ctx.task_title}' if self.ctx.task_title else ''}",
            lead=f"{question.get('title') or 'Carry on?'} {question.get('message') or ''}".strip(),
            points=[f"{r['name']}: {r['headline']}" for r in results[:index] if r.get("headline") and r["status"] == "done"],
            button="Answer it",
        )
        # The answer may come back from Teams instead, through /agents/teams/answer.
        await self.ask_in_teams(question, results, index)
        try:
            await asyncio.wait_for(self.answered.wait(), ANSWER_TIMEOUT.total_seconds())
        except TimeoutError as exc:
            raise Stop("No answer came within a day.") from exc
        self.run.status = "running"
        answer = self.answer or {"go": False, "text": ""}
        if self.stopping:
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
        if kind in ("ask_me", "get_approval", "check"):
            return await handler(cfg, spent, results, index)
        return await handler(cfg, spent)

    def start(self, kind: str) -> Out:
        who = self.user.display_name
        where = f" on “{self.ctx.task_title}”" if self.ctx.task_title else ""
        out = Out(headline=f"Started by {who}{where}")
        if kind != "start_manual":
            out.notes.append(f"“{KINDS[kind][0]}” will start it on its own once triggers are switched on. This run was started by hand.")
        if self.ctx.task_title:
            out.sources.append(source("Proposals list", self.ctx.task_title))
        return out

    async def s_read_documents(self, cfg: dict[str, Any], spent: Spent) -> Out:
        ctx, deps = self.ctx, self.deps
        if not ctx.task_id and not self.uploads:
            return skipped("No task was chosen and no files were added, so there was nothing to read.")
        which = _s(cfg.get("which")) or "all"
        found: list[tuple[str, str, str | None, Any]] = []  # name, origin, url, fetch
        notes: list[str] = []

        if ctx.task_id:
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
                        for f in await deps.drive.list_files(folder.name):
                            if reading.is_own_output(f["name"], f["path"], deps.settings.enquiry_report_folder):
                                continue
                            found.append((f["name"], f"task folder: {f['path']}", f.get("web_url"), ("drive", f["id"])))
                    else:
                        notes.append("The task has no folder in the Proposal Team Channel yet.")
                except DriveError as exc:
                    notes.append(f"The task folder could not be read: {exc}")

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
        notes = ["Drafts only — nothing was sent. Copy one, or open it in your mail, and send it yourself."]
        if missing:
            notes.append(f"{missing} recipient(s) have no email address on record — {NOT_GIVEN}.")
        return Out(
            headline=f"{len(drafts)} draft email(s), {how}",
            output={"drafts": drafts}, notes=notes,
            sources=[s["source"] for s in ctx.suppliers] if to == "suppliers" else [],
        )

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
        notes = ["Email to yourself comes later; this went to the bell in the app."] if _s(cfg.get("via")) in ("email", "both") else []
        return Out(headline="You were told in the app", output={"title": title, "message": message}, notes=notes)

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


# ── starting ───────────────────────────────────────────────────────────


def start(deps: Deps, run_id: uuid.UUID, uploads: list[tuple[str, bytes, str | None]]) -> Runner:
    runner = Runner(deps, run_id, uploads)
    task = asyncio.create_task(runner.go(), name=f"agent-run-{run_id}")
    runner.task = task
    RUNNING[run_id] = runner
    task.add_done_callback(lambda _t: RUNNING.pop(run_id, None))
    return runner


def settle_orphan(run: AgentRun) -> bool:
    """A run this process is not running, left marked as live: say it was cut off."""
    if run.status not in ("running", "waiting") or run.id in RUNNING:
        return False
    run.status = "failed"
    run.error = "The run was cut off (the server restarted). Run it again."
    run.summary = run.summary or run.error
    run.question = None
    run.finished_at = datetime.now(UTC)
    return True


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
    *, agent: str, task: str | None, question: dict[str, Any], about: dict[str, Any] | None, link: str
) -> dict[str, Any]:
    """The Adaptive Card the flow posts: the question, what it is about, a note box, two buttons.

    "Post adaptive card and wait for a response" hands back ``data.go`` (from
    the button pressed) and ``data.note`` (from the box).
    """
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": f"{agent} needs your answer", "weight": "Bolder", "size": "Medium", "wrap": True},
    ]
    if task:
        body.append({"type": "TextBlock", "text": task, "isSubtle": True, "spacing": "None", "wrap": True})
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
