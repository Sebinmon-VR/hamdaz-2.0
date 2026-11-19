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
    ("task_files", _tool(
        "task_files",
        "A task's files (by task id, tender number or a few words of its title), from its "
        "task folder and its Proposals list item. which=\"all\" or words of the names "
        "(\"rfq\", \"specs boq\") SENDS those files into the chat as file cards at once; "
        "which=\"\" only lists them. For \"send me the RFQ of 1958\", \"attachments for "
        "the zebra one\", \"the files\".",
        {"query": {"type": "string"},
         "which": {"type": "string", "description": "\"all\", words of file names, or "
                                                     "\"\" to only list"}})),
    ("costing_ask", _tool(
        "file_to_task",
        "Put a costing sheet they sent in this chat into THEIR task's folder (named so it "
        "reads as the costing), so the task has its costing. Use it when they send a "
        "costing sheet made outside the ERP for a task (often after you asked for one).",
        {"task_id": {"type": "string"},
         "file_names": {"type": "array", "items": {"type": "string"},
                        "description": "As chat_files names them"}})),
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


#: Features tried on one person first (IT_SUPPORT_TRY_TO) before everyone.
TRYING: Final = frozenset({"task_files", "costing_ask"})


def offered(settings: Settings, email: str = "") -> list[dict[str, Any]]:
    """The tools of the features on, for this person: one being tried is
    theirs only if they're the one it's tried on."""
    tester = (getattr(settings, "it_support_try_to", "") or "").strip().lower()
    return [tool for key, tool in TOOLS if features.on(settings, key)
            and not (key in TRYING and tester and email.strip().lower() != tester)]


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
        #: A task's files and costing (``costcheck.Files``), the worker's.
        self.files: Any = None
        #: The sent ledger (the worker's): a costing filed here isn't asked for.
        self.sent: Any = None
        #: After "submitted" is filed: what to add about its costing (the
        #: worker's; "" when it has one or the ask is off).
        self.on_submitted: Callable[[Person, Any], Awaitable[str]] | None = None

    async def run(self, turn: Turn, name: str, args: dict[str, Any]) -> str:
        if self._factory is None:
            return "Not available here (no database)."
        person = turn.person
        try:
            if name == "update_task":
                return await self._update(turn, args)
            if name == "find_tender":
                return await self._find(turn, args["query"])
            if name == "task_files":
                return await self._task_files(turn, args["query"], args.get("which", ""))
            if name == "file_to_task":
                return await self._file_to_task(turn, args["task_id"], args["file_names"])
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
        costing = ""
        if filed.ok and update.submission and not update.submission.startswith("Not") \
                and self.on_submitted is not None:
            costing = await self.on_submitted(person, mine[task_id])
        return (("Filed: " if filed.ok else "Not filed: ") + f"{task_id} {filed.said}."
                + (" The Proposals list itself isn't changed: tell them to update it there."
                   if filed.ok and filed.on_list else "") + costing)

    # ── tenders ──

    def _sees_proposals(self, person: Person) -> bool:
        return person.access.is_admin or "proposals" in person.access.modules

    async def _match(self, session: AsyncSession, query: str) -> list[Any]:
        """The tasks a number or a few words of a title mean, at most three."""
        from app.mailwatch import matching
        from app.models.proposal_index import ProposalIndexItem

        numbers = re.findall(r"\d{3,}", query)
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
        return rows[:3]

    async def _file_to_task(self, turn: Turn, task_id: str, names: list[str]) -> str:
        """Costing sheets sent in the chat, into the person's own task's folder."""
        from app.itsupport import activity, costcheck

        person = turn.person
        if not any(t["name"] == "file_to_task" for t in offered(self._settings, person.email)):
            return "Filing a costing sheet isn't switched on for them yet."
        task = {t.id: t for t in person.tasks}.get(task_id.strip())
        if task is None:
            return (f"{task_id} isn't one of their open tasks, so I can't file into it: only "
                    "the holder's own tasks. Ask which task it's for.")
        if self.files is None:
            return "Task folders can't be reached here."
        if not costcheck.may_try(self._settings, person.email, task):
            return ("While this is being tried, sheets go only into the tester's own tasks with "
                    "\"test\" in the title. Tell them so.")
        opened = [turn.files[n] for n in names if n in turn.files]
        if not opened:
            return "None of those files are in the chat. See chat_files."
        saved = []
        for o in opened:
            data = (o.readable.data if o.readable else None) or o.data
            if not data:
                saved.append(f"{o.name}: couldn't be read")
                continue
            try:
                f = await self.files.save(task, o.name, data, o.media_type)
            except Exception as exc:  # noqa: BLE001 - say what went wrong
                logger.exception("it support: filing %s into %s failed", o.name, task.id)
                saved.append(f"{o.name}: not saved ({exc})"[:200])
                continue
            saved.append(f"saved as [{f.name}]({f.link})")
            turn.done.append(f"filed {f.name} into {task.id}'s folder")
            await activity.record(self._factory, email=person.email, kind=activity.CHANGE,
                                  summary=f"costing sheet {f.name} put in {task.id}'s folder",
                                  detail={"task": task.id, "file": f.name})
        if any(s.startswith("saved") for s in saved) and self.sent is not None:
            tester = (getattr(self._settings, "it_support_try_to", "") or "").strip().lower()
            await self.sent.mark(costcheck.key(task.id, test=bool(tester)), email=person.email,
                                 kind="costing-ask", detail={"filed": True})
        turn.focus = (task.id, task.title or "", person.first_name, person.email)
        return f"{task.id}: " + "; ".join(saved) + (". It now counts as the task's costing; "
                                                     "tell them in a line, with the link.")

    async def _task_files(self, turn: Turn, query: str, which: str = "") -> str:
        person = turn.person
        if not self._sees_proposals(person):
            return "They don't have the Proposals module, so a task's files can't be fetched."
        if self.files is None:
            return "Task files can't be read here."
        if not any(t["name"] == "task_files" for t in offered(self._settings, person.email)):
            return "Fetching a task's files isn't switched on for them yet."
        from app.itsupport import costcheck

        said = query.strip()
        task = {t.id: t for t in person.tasks}.get(said)
        if task is not None:
            task_id, title = task.id, task.title or ""
            turn.focus = (task.id, title, person.first_name, person.email)
        else:
            assert self._factory is not None
            async with self._factory() as session:
                rows = await self._match(session, said)
            if not rows:
                return "No task matches that on the Proposals list."
            exact = next((r for r in rows if r.item_id == said), None)
            if exact is None and len(rows) > 1:
                return _json({"several": [{"id": r.item_id, "title": r.title,
                                           "holder": r.assigned_name} for r in rows],
                              "note": "Ask which one, naming them."})
            row = exact or rows[0]
            task_id, title = row.item_id, row.title or ""
            turn.focus = (row.item_id, title, row.assigned_name or "",
                          (row.assigned_email or "").lower())
        files, folder, problems = await self.files.of(task or task_id)
        if not files and not problems:
            return _json({"task": {"id": task_id, "title": title}, "folder": folder,
                          "files": [], "note": "No files in its task folder or on the list. "
                          "Say so, with the folder link if there is one."})
        out = costcheck.for_model(task, task_id, title, files, folder, problems)
        wanted = picked(files, which)
        if wanted and turn.on_files is not None:
            cards, links = [], []
            for f in wanted[:MAX_CARDS]:
                card = await self.files.card(f)
                (cards.append(card) if card else links.append(f))
            if cards:
                try:
                    await turn.on_files(cards, f"{task_id}'s files 👇")
                    turn.done.append(f"sent {len(cards)} file card(s) of {task_id}")
                except Exception as exc:  # noqa: BLE001 - the links still get them there
                    logger.warning("it support: Teams refused the file cards: %s", exc)
                    by_name = {f.name: f for f in wanted}
                    links += [by_name.get(c[2]) or costcheck.File(c[2], "", c[1])
                              for c in cards]
                    cards = []
            out["sent_as_cards"] = [c[2] for c in cards]
            out["give_as_links"] = [{"name": f.name, "link": f.link} for f in links]
            out["how_to_give"] = (
                "The files in sent_as_cards are in the chat now as file cards: don't list "
                "or link them again, just say in a few words they're there. Give any in "
                "give_as_links as [name](link)." + (f" {len(wanted) - MAX_CARDS} more "
                                                     "matched: give the folder link for those."
                                                     if len(wanted) > MAX_CARDS else ""))
        elif which.strip():
            out["note"] = "No file name matched what they asked for: say so and list a few."
        return _json(out)

    async def _find(self, turn: Turn, query: str) -> str:
        person = turn.person
        if not self._sees_proposals(person):
            return "They don't have the Proposals module, so tenders can't be looked up."
        from app.models.task_folder import TaskFolder
        from app.quoting.service import quotes_for_tasks

        assert self._factory is not None
        async with self._factory() as session:
            rows = await self._match(session, query)
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


#: File cards in one message, at most.
MAX_CARDS: Final = 10


def picked(files: list[Any], which: str) -> list[Any]:
    """The files they asked for: all, or those whose name has any of the
    words ("rfq", "specs boq"); none for "" (only a list)."""
    which = which.strip().casefold()
    if not which:
        return []
    if which in ("all", "everything", "all files"):
        return list(files)
    words = [w for w in re.findall(r"[a-z0-9]{2,}", which) if w not in ("the", "and", "file",
                                                                        "files", "of")]
    return [f for f in files if any(w in f.name.casefold() for w in words)]


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

