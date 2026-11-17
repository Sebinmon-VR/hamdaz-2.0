"""What the person wants done with the files they sent.

The user's ask (2026-11-13): "let the agent understand the goal, like how
Claude does": a customer's RFQ sent with "can you check this" wants its
requirements read; three supplier quotations want comparing for the costing;
an RFQ with "need suppliers for this" wants its items read and then sourced.

**Rules first, the model for the rest** (the user's standing rule). When every
file's first look agrees on its kind, and the message says nothing against
it, the goal follows from that. When the files disagree, the first look can't
tell, the words point elsewhere, or the files name several of the person's
tasks, Claude (the agents' model, Haiku) is shown what a colleague would
see — the message, each file's first look and opening words, the recent
chat, the person's open tasks — and answers in a fixed shape. When even that
is unsure, the goal is to ask one short question.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from app.agents.model import AgentModel, Spent
from app.followups import service as fu
from app.itsupport import files as files_mod
from app.itsupport.work import Work, at
from app.reminders.service import is_done

logger = logging.getLogger("hamdaz.itsupport")

GOALS: Final = ("requirements", "costing", "chat", "ask")

#: Words that ask for suppliers to be found for what was sent.
_FIND = re.compile(
    r"\b(find|search|look\s*for|source|get)\b.{0,40}\b(suppliers?|distributors?|vendors?|"
    r"sellers?|dealers?)\b", re.IGNORECASE,
)
#: Words that ask for the comparison or the costing.
_COSTING = re.compile(r"\b(costing|compare|comparison|cheapest|best\s+price|margin)\b",
                      re.IGNORECASE)

SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "enum": list(GOALS)},
        "task_id": {"type": "string", "description": "The task id from the list, or empty."},
        "find_suppliers": {"type": "boolean"},
        "question": {"type": "string",
                     "description": "When goal is ask: the one short question to ask, else empty."},
        "why": {"type": "string", "description": "A few words, for the log."},
    },
    "required": ["goal", "task_id", "find_suppliers", "question", "why"],
    "additionalProperties": False,
}

SYSTEM: Final = """\
You work out what a colleague in the presales team wants done with the files they just sent
to IT Support in Teams. You see their message, a first look at each file, the recent chat
and their open tasks.

Goals:
- requirements: the files are what a CUSTOMER asks for (RFQ, tender, BOQ, specification,
  purchase requisition) and they want it read: items, quantities, conditions, deadline.
  Set find_suppliers when they also want suppliers or distributors found for the items.
- costing: the files are SUPPLIERS' quotations or offers (prices from a vendor to us) and
  they want them read and compared for the costing.
- chat: the files aren't for either (a screenshot of an error, a photo, a question about a
  file); the normal chat answers.
- ask: you can't tell which, or which task it is for matters and is unclear. Write the one
  short, casual question you'd ask, naming the options ("Is this the customer's RFQ or a
  supplier's quote?", "Is this for 6000156007 or the OB8 one?").

A quotation is FROM a supplier TO us (Hamdaz); a request for quotation is FROM the customer.
Their words win over the file's look when they are clear. task_id: the one task from their
list the files are for (by RFQ/tender/PR number, product or what they said), else empty."""


@dataclass(frozen=True, slots=True)
class Goal:
    goal: str
    task_id: str | None = None
    find_suppliers: bool = False
    question: str = ""
    why: str = ""
    by_model: bool = False


def task_lines(work: Work | None, now: datetime) -> str:
    if work is None:
        return "(their tasks couldn't be read)"
    lines = []
    for task in work.tasks:
        due = fu.due_of(task)
        if is_done(task) or due is None or due < now:
            continue
        lines.append(f"- id {task.id}: {(task.title or '').strip()[:100]} (BCD {at(due, now)})")
    return "\n".join(lines[:40]) or "(no open tasks)"


def by_rules(text: str, opened: list[files_mod.Opened]) -> Goal | None:
    """The goal when the files and the words settle it; None when not."""
    readable = [o for o in opened if o.readable is not None]
    if not readable:
        return None
    kinds = {o.looks_like for o in readable}
    if "" in kinds or len(kinds) != 1:
        return None
    tasks = {tid for o in readable for tid in o.task_ids[:1]}
    if len(tasks) > 1:
        return None
    task_id = next(iter(tasks), None)
    kind = kinds.pop()
    find = bool(_FIND.search(text))
    if kind == "requirement" and not _COSTING.search(text):
        return Goal("requirements", task_id, find, why="customer documents, by their look")
    if kind == "supplier_quote" and not find:
        return Goal("costing", task_id, why="supplier quotations, by their look")
    return None


async def decide(
    model: AgentModel, *, text: str, opened: list[files_mod.Opened], history: str,
    work: Work | None, now: datetime, user_key: str, spent: Spent,
) -> Goal:
    if (settled := by_rules(text, opened)) is not None:
        return settled
    if not model.configured:
        return Goal("ask", question="Is this the customer's request, or quotes from suppliers?",
                    why="no model to tell")
    content = [{"type": "text", "text": (
        f"Their message: {text or '(no words, just the files)'}\n\n"
        f"The files:\n" + "\n".join(files_mod.describe(o) for o in opened) +
        f"\n\nRecent chat (oldest first):\n{history or '(none)'}\n\n"
        f"Their open tasks:\n{task_lines(work, now)}"
    )}]
    answer = await model.ask(system=SYSTEM, content=content, schema=SCHEMA, user_key=user_key,
                             spent=spent, max_tokens=600)
    goal = answer.get("goal") if answer.get("goal") in GOALS else "ask"
    known = {t.id for t in (work.tasks if work else [])}
    task_id = str(answer.get("task_id") or "").strip()
    return Goal(
        goal, task_id if task_id in known else None, bool(answer.get("find_suppliers")),
        str(answer.get("question") or "").strip()
        or "Is this the customer's request, or quotes from suppliers?",
        str(answer.get("why") or ""), by_model=True,
    )
