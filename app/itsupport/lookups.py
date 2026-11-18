"""The brain's own tools beyond the ERP's routes: exact answers, by rules.

The user's shortlist (2026-11-18): some answers need more than one route, or
a search the ERP has no route for, or arithmetic a model shouldn't do. Each
tool here is deterministic, reads only what the person may see, and is
offered to the model only while its feature is on (``features``):

* ``update_task`` (status_updates): file a status or a reason (``status``);
* ``find_tender`` (tender_lookup): one tender by number or words, with its
  folder and costing;
* ``search_bids`` (past_bids): past tasks like this, and who bid on them;
* ``past_prices`` (price_memory): what suppliers quoted us for an item;
* ``convert_currency`` (currency): at Zoho Books' rate, worked out exactly;
* ``erp_guide`` (howto): how to do something in the ERP, with the page;
* ``connect_colleague`` (connect_people): for someone who oversees a team,
  a Teams call link to a teammate, or a message to them from IT Support
  (after their yes) whose answer comes back to them.

* ``customer_pos`` (projects): customer POs from the project tracker list
  (``tracker``), super admins only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.itsupport import features, guide, status, when

if TYPE_CHECKING:
    from app.itsupport.brain import Person, Turn
    from app.itsupport.erp import ERP

logger = logging.getLogger("hamdaz.itsupport")

_STATUS_CHOICES: Final = ["", "Not Started", "In Progress", "Completed", "On Hold"]
_SUBMISSION_CHOICES: Final = ["", "Submitted", "Not Submitted"]


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties,
                             "required": list(properties), "additionalProperties": False}}


#: Each tool with the feature that offers it.
TOOLS: Final[list[tuple[str, dict[str, Any]]]] = [
    ("status_updates", _tool(
        "update_task",
        "File their update on one of THEIR OWN tasks: a Status, a Submission Status, a note "
        "(the reason it's late, or a remark), or use_remarks to answer a late-task question "
        "with the task's Remarks. Filed at once (they said it), reported to their managers "
        "like the web form; the Proposals list itself isn't changed. Leave a field \"\" "
        "when they didn't say it.",
        {"task_id": {"type": "string", "description": "The Proposals task id, e.g. 1952"},
         "status": {"type": "string", "enum": _STATUS_CHOICES},
         "submission_status": {"type": "string", "enum": _SUBMISSION_CHOICES},
         "note": {"type": "string", "description": "Their words: the reason or remark"},
         "use_remarks": {"type": "boolean"}})),
    ("tender_lookup", _tool(
        "find_tender",
        "One tender by its number (tender/RFQ/PR/task id) or a few words of its title: who "
        "holds it, BCD, status, end user, remarks, its folder, and its costing if one exists.",
        {"query": {"type": "string"}})),
    ("past_bids", _tool(
        "search_bids",
        "Past tasks like this one (by words of the title, optionally one end user), newest "
        "first, and who on the team has bid on similar ones.",
        {"words": {"type": "string"}, "end_user": {"type": "string",
                                                   "description": "\"\" for any"}})),
    ("price_memory", _tool(
        "past_prices",
        "What suppliers quoted us before for an item (brand, model, part number or "
        "description): supplier, unit price, currency, date, and the costing it was for.",
        {"item": {"type": "string"}})),
    ("currency", _tool(
        "convert_currency",
        "Convert an amount at Zoho Books' rate (the rate costings use). Never convert "
        "yourself.",
        {"amount": {"type": "string", "description": "e.g. 4500 or 4,500.50"},
         "from_currency": {"type": "string", "description": "ISO code, e.g. USD"},
         "to_currency": {"type": "string", "description": "ISO code, e.g. AED"}})),
    ("projects", _tool(
        "customer_pos",
        "Customer POs from the project tracker (super admins only): by customer, PO number, "
        "Ref, sales order or words of the description. Stage, status, delivery dates, "
        "invoice and payment status, progress and concerns.",
        {"query": {"type": "string"}})),
    ("howto", _tool(
        "erp_guide",
        "How to do something in the ERP (raise a costing, answer a reminder, apply for "
        "leave…): the steps and the page's link. Use it for any 'how do I' about the app.",
        {"topic": {"type": "string"}})),
    ("connect_people", _tool(
        "connect_colleague",
        "Only for someone who oversees a team, about a teammate: how='call' gives a Teams "
        "call link (and a chat link) at once; how='message' asks the teammate from IT "
        "Support (text = what to ask them, naming who's asking and the task) and their "
        "answer is passed back. A message waits for the person's yes, like a change.",
        {"email": {"type": "string", "description": "The teammate's email, or their "
                                                    "name as the person said it"},
         "how": {"type": "string", "enum": ["call", "message"]},
         "task_id": {"type": "string", "description": "\"\" if not about one task"},
         "text": {"type": "string", "description": "For a message: what to ask; else \"\""}})),
]
NAMES: Final = {tool["name"] for _, tool in TOOLS}


def offered(settings: Settings) -> list[dict[str, Any]]:
    return [tool for key, tool in TOOLS if features.on(settings, key)]


def link_base(settings: Settings) -> str:
    return (settings.it_support_link_url or settings.frontend_url).rstrip("/")


def teams_call(email: str) -> str:
    return f"https://teams.microsoft.com/l/call/0/0?users={quote(email)}"


def teams_chat(email: str) -> str:
    return f"https://teams.microsoft.com/l/chat/0/0?users={quote(email)}"


class Lookups:
    def __init__(self, settings: Settings, erp: ERP,
                 factory: async_sessionmaker[AsyncSession] | None) -> None:
        self._settings = settings
        self._erp = erp
        self._factory = factory
        #: Sends a planned Teams message now (the worker's): for a message
        #: they already said yes to.
        self.on_teams: Callable[[Person, Any], Awaitable[Any]] | None = None
        #: The Proposals site reader, for the project tracker list.
        self.sharepoint: Any = None

    async def run(self, turn: Turn, name: str, args: dict[str, Any]) -> str:
        if self._factory is None:
            return "Not available here (no database)."
        person = turn.person
        try:
            if name == "update_task":
                return await self._update(turn, args)
            if name == "find_tender":
                return await self._find(turn, args["query"])
            if name == "search_bids":
                return await self._bids(person, args["words"], args["end_user"])
            if name == "past_prices":
                return await self._prices(person, args["item"])
            if name == "convert_currency":
                return await self._convert(person, args)
            if name == "customer_pos":
                return await self._pos(person, args["query"])
            if name == "erp_guide":
                return guide.answer(args["topic"], link_base(self._settings))
            if name == "connect_colleague":
                return await self._connect(turn, args)
        except Exception as exc:  # noqa: BLE001 - say what went wrong; the turn goes on
            logger.exception("it support: %s failed", name)
            return f"That didn't work: {type(exc).__name__}: {exc}"[:500]
        return f"No tool called {name}."

    # ── status ──

    async def _update(self, turn: Turn, args: dict[str, Any]) -> str:
        from app.models.followup import FollowupStatus, TaskFollowup

        person = turn.person
        task_id = args["task_id"].strip()
        mine = {t.id: t for t in person.tasks}
        if task_id not in mine:
            return (f"{task_id} isn't one of their open tasks, so it can't be updated from "
                    "here (only the holder updates a task).")
        if features.trial(self._settings) and \
                "test" not in (mine[task_id].title or "").casefold():
            return "Trial: only tasks with \"test\" in the title are updated. Tell them so."
        update = status.Update(task_id, args["status"] or None,
                               args["submission_status"] or None, args["note"].strip(),
                               bool(args["use_remarks"]))
        if update.empty:
            return "Nothing to file: no status, submission status or note."
        assert self._factory is not None
        async with self._factory() as session:
            pending = {r.task_id: str(r.id) for r in (await session.scalars(
                select(TaskFollowup).where(TaskFollowup.assignee_id == person.user_id,
                                           TaskFollowup.status == FollowupStatus.PENDING,
                                           TaskFollowup.task_id == task_id))).all()}
        filed = await status.file(self._erp, user_id=person.user_id, access=person.access,
                                  update=update, task=mine[task_id], pending_reason=pending,
                                  now=datetime.now(UTC))
        turn.done.append(f"update {task_id}: {filed.said}")
        turn.focus = (task_id, mine[task_id].title or "", person.first_name, person.email)
        turn.filed.append(filed)
        return (("Filed: " if filed.ok else "Not filed: ") + f"{task_id} {filed.said}."
                + (" The Proposals list itself isn't changed: tell them to update it there."
                   if filed.ok and filed.on_list else ""))

    # ── tenders ──

    def _sees_proposals(self, person: Person) -> bool:
        return person.access.is_admin or "proposals" in person.access.modules

    async def _find(self, turn: Turn, query: str) -> str:
        person = turn.person
        if not self._sees_proposals(person):
            return "They don't have the Proposals module, so tenders can't be looked up."
        from app.mailwatch import matching
        from app.models.proposal_index import ProposalIndexItem
        from app.models.task_folder import TaskFolder
        from app.quoting.service import quotes_for_tasks

        numbers = re.findall(r"\d{3,}", query)
        assert self._factory is not None
        async with self._factory() as session:
            rows: list[ProposalIndexItem] = []
            ids = [n for n in numbers if len(n) <= 6]
            if ids:
                rows = list((await session.scalars(select(ProposalIndexItem).where(
                    ProposalIndexItem.item_id.in_(ids),
                    ProposalIndexItem.deleted.is_(False)))).all())
            if not rows and numbers:
                rows = await matching.by_number(session, numbers)
            if not rows:
                found = await matching.by_words(session, query, customer=None, numbers=set())
                if found:
                    rows = list((await session.scalars(select(ProposalIndexItem).where(
                        ProposalIndexItem.item_id.in_([c.item_id for c in found[:3]])))).all())
            rows = rows[:3]
            if not rows:
                return "No task matches that on the Proposals list."
            task_ids = [r.item_id for r in rows]
            folders = {f.item_id: f.web_url for f in (await session.scalars(
                select(TaskFolder).where(TaskFolder.item_id.in_(task_ids)))).all()}
            quotes = await quotes_for_tasks(session, task_ids)
        now = datetime.now(UTC)
        base = link_base(self._settings)
        out = []
        for r in rows:
            q = quotes.get(r.item_id)
            out.append({
                "id": r.item_id, "title": r.title, "holder": r.assigned_name,
                "holder_email": r.assigned_email,
                "end_user": r.end_user, "status": r.status or r.effective_status,
                "submission": r.submission_status,
                "bcd": when.said_day(r.bid_closing_date, now) if r.bid_closing_date else None,
                "remarks": " ".join((r.remarks or "").split())[:400],
                "folder": folders.get(r.item_id),
                "costing": {"reference": q.reference, "status": str(q.status.value
                            if hasattr(q.status, "value") else q.status),
                            "link": f"{base}/quote-requests/{q.id}"} if q else None,
            })
        first = rows[0]
        turn.focus = (first.item_id, first.title or "", first.assigned_name or "",
                      (first.assigned_email or "").lower())
        return _json({"tasks": out, "note": "BCD from the app's copy of the list (date only); "
                      "the person's own task list in the context has the exact time."})

    async def _bids(self, person: Person, words: str, end_user: str) -> str:
        if not self._sees_proposals(person):
            return "They don't have the Proposals module."
        from app.mailwatch import matching
        from app.models.proposal_index import ProposalIndexItem

        assert self._factory is not None
        async with self._factory() as session:
            found = await matching.by_words(session, words, customer=end_user or None,
                                            numbers=set())
            if found:
                listed = [{"id": c.item_id, "title": c.title, "holder": c.assignee,
                           "end_user": c.customer, "status": c.status,
                           "created": (c.created_at or "")[:10]} for c in found]
            else:
                # The word search needs two words or more ("robots" alone found
                # nothing, 2026-11-18): every word in the title instead, a
                # plural taken as its singular, and the end user if given.
                terms = [w[:-1] if len(w) > 4 and w.lower().endswith("s") else w
                         for w in re.findall(r"[A-Za-z0-9]{3,}", words)
                         if w.lower() not in _STOP][:4]
                query = select(ProposalIndexItem).where(ProposalIndexItem.deleted.is_(False))
                for term in terms:
                    query = query.where(ProposalIndexItem.title.ilike(f"%{term}%"))
                if end_user:
                    query = query.where(ProposalIndexItem.end_user.ilike(f"%{end_user}%"))
                rows = (await session.scalars(
                    query.order_by(ProposalIndexItem.sp_created_at.desc().nulls_last())
                    .limit(15))).all() if (terms or end_user) else []
                listed = [{"id": r.item_id, "title": r.title, "holder": r.assigned_name,
                           "end_user": r.end_user, "status": r.status or r.effective_status,
                           "submission": r.submission_status,
                           "created": r.sp_created_at.date().isoformat()
                           if r.sp_created_at else None} for r in rows]
        if not listed:
            return "Nothing like that on the Proposals list (try other words)."
        who: dict[str, int] = {}
        for row in listed:
            if row.get("holder"):
                who[row["holder"]] = who.get(row["holder"], 0) + 1
        return _json({"tasks": listed, "who_has_bid_on_these": dict(
            sorted(who.items(), key=lambda kv: -kv[1]))})

    # ── prices ──

    async def _prices(self, person: Person, item: str) -> str:
        allowed = person.access.is_admin or {"quotes", "quote_requests"} & person.access.modules
        if not allowed:
            return "They don't have the quotes module, so past prices can't be shown."
        from app.models.comparison import SupplierQuote, SupplierQuoteItem
        from app.models.quoting import QuoteRequest

        tokens = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-./]{1,}", item)
                  if len(t) >= 3 and t.lower() not in _STOP][:4]
        if not tokens:
            return "Say the item: a brand, model, part number or a few words."
        conditions = [or_(SupplierQuoteItem.description.ilike(f"%{t}%"),
                          SupplierQuoteItem.brand.ilike(f"%{t}%"),
                          SupplierQuoteItem.part_number.ilike(f"%{t}%")) for t in tokens]
        assert self._factory is not None
        async with self._factory() as session:
            rows = (await session.execute(
                select(SupplierQuoteItem, SupplierQuote)
                .join(SupplierQuote, SupplierQuoteItem.quote_id == SupplierQuote.id)
                .where(*conditions)
                .order_by(SupplierQuoteItem.created_at.desc()).limit(15))).all()
            comparison_ids = {sq.comparison_id for _, sq in rows if sq.comparison_id}
            costings = {q.comparison_id: q for q in (await session.scalars(
                select(QuoteRequest).where(QuoteRequest.comparison_id.in_(comparison_ids))
            )).all()} if comparison_ids else {}
        if not rows:
            return f"No supplier quote line mentions {item!r}."
        base = link_base(self._settings)
        out = []
        for line, sq in rows:
            costing = costings.get(sq.comparison_id)
            out.append({
                "supplier": sq.supplier_name, "item": line.description,
                "part": line.part_number, "brand": line.brand,
                "qty": str(line.quantity) if line.quantity is not None else None,
                "unit_price": str(line.unit_price) if line.unit_price is not None else None,
                "currency": sq.currency, "quote_date": sq.quote_date,
                "received": line.created_at.date().isoformat() if line.created_at else None,
                "costing": f"{costing.reference} {base}/quote-requests/{costing.id}"
                if costing else None,
            })
        return _json({"lines": out})

    # ── currency ──

    async def _convert(self, person: Person, args: dict[str, Any]) -> str:
        try:
            amount = Decimal(args["amount"].replace(",", "").strip())
        except (InvalidOperation, AttributeError):
            return "That amount isn't a number."
        source = args["from_currency"].strip().upper()
        target = args["to_currency"].strip().upper()
        if source == target:
            return f"{amount:,.2f} {target}"
        answer = await self._erp.call(person.user_id, "GET", "/quote-requests/fx-rate",
                                      access=person.access,
                                      query=f"quote={target}&supplier={source}")
        if not answer.ok or not isinstance(answer.body, dict):
            return f"Zoho's rate couldn't be read: {answer.for_model()[:300]}"
        # rate = units of the source (supplier side) per one unit of the target.
        rate = Decimal(str(answer.body["rate"]))
        converted = (amount / rate).quantize(Decimal("0.01"))
        dated = answer.body.get("effective_date")
        return (f"{amount:,.2f} {source} = {converted:,.2f} {target} at Zoho's rate "
                f"(1 {target} = {rate} {source}{', as of ' + str(dated) if dated else ''}).")

    # ── customer POs ──

    async def _pos(self, person: Person, query: str) -> str:
        if not person.access.is_admin:
            return "Customer POs are for super admins only, for now."
        if self.sharepoint is None:
            return "The project tracker can't be read here."
        from app.itsupport import tracker

        found = tracker.find(await tracker.rows(self.sharepoint, self._settings), query)
        if not found:
            return "No PO in the project tracker matches that."
        return _json({"pos": found, "note": "From the project tracker list; amounts in AED. "
                      "Dates are as the list has them."})

    # ── people ──

    async def _connect(self, turn: Turn, args: dict[str, Any]) -> str:
        from app.itsupport.brain import Step

        person = turn.person
        email, why = teammate(args["email"], person.team_emails, person.team_names)
        if email is None and turn.focus is not None and turn.focus[3] in person.team_emails \
                and args["email"].strip().lower() in ("", turn.focus[2].lower(),
                                                      turn.focus[3]):
            email = turn.focus[3]
        if email is None:
            return why
        if args["how"] == "call":
            name = next((n.title() for n, e in person.team_names.items() if e == email),
                        email.split("@")[0])
            first = name.split(" ")[0]
            return ("Give them exactly this line (it shows as a link that starts the call): "
                    f"[📞 Call {first}]({teams_call(email)})   [💬 Chat]({teams_chat(email)})")
        text = args["text"].strip()
        if not text:
            return "Say what to ask them (text)."
        step = Step("TEAMS", f"message:{email}", args["task_id"].strip(), {"text": text}, [],
                    {}, f"message {email} from IT Support: {text[:120]}")
        if turn.agreed and self.on_teams is not None:
            # They already said yes to messaging them: send it now.
            sent = await self.on_teams(person, step)
            turn.done.append(step.what)
            return f"Sent. {sent.for_model()[:300]}"
        turn.plan.steps.append(step)
        return ("Added as a step, not sent yet. Ask them once in one line (\"want me to "
                "message <name>: '<text>'?\"); it goes on their yes.")


def teammate(said: str, emails: frozenset[str],
             names: dict[str, str]) -> tuple[str | None, str]:
    """The teammate meant, by email or by name ("Krish" → krishnendu@…), and
    why not when it can't be one."""
    said = said.strip().lower()
    if not emails:
        return None, ("Not theirs to connect: only someone who oversees a team can, and only "
                      "with a teammate.")
    if said in emails:
        return said, ""
    word = said.split("@")[0].replace(".", " ").strip()
    found = sorted({email for name, email in names.items()
                    if word and (word in name or name.split(" ")[0] in word
                                 or word in email.split("@")[0])})
    if len(found) == 1:
        return found[0], ""
    if len(found) > 1:
        return None, f"Several teammates match {said!r}: {', '.join(found)}. Ask which one."
    return None, f"{said} isn't in a team they oversee, so I can't connect them."


_STOP: Final = frozenset({"the", "and", "for", "with", "pcs", "nos", "unit", "units", "item",
                          "items", "supply", "of"})


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)[:12_000]

