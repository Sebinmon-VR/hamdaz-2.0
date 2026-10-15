"""The companion's "what is this?": a part of a screen, explained.

The person draws a box over part of a page; the browser sends the text inside
it (not a picture — text is a fraction of the tokens) with where they are.

Cheapest first, because the user asked for it free (2026-10-15):

1. **The glossary.** The app's own words — BCD, landed cost, effective status
   — have one meaning here, and saying it costs nothing.
2. **A free model.** Groq (and whatever else LLM_PROVIDERS lists), through the
   app's ``TextModel`` with Claude left out.
3. **Haiku**, only when every free model is busy, and only while
   ``AGENT_EXPLAIN_PAID_FALLBACK`` is on.

Each is told to use only the selected text and the glossary entries that
appear in it — not the whole glossary, which was most of the tokens — and to
say when it cannot tell.
"""

from __future__ import annotations

import re
from typing import Any, Final

from app.agents.model import AgentModel, Spent
from app.core.llm import TextModel

#: The app's words, as this company uses them.
GLOSSARY: Final[dict[str, str]] = {
    "bcd": "Bid closing date — the last day a bid can be submitted. Late tasks are judged against it, falling back to the due date.",
    "bid closing date": "The last day a bid can be submitted. Late tasks are judged against it, falling back to the due date.",
    "rfq": "Request for quotation — a customer asks us for prices, or we ask suppliers for theirs.",
    "rfp": "Request for proposal — a tender asking for a full proposal, not just prices.",
    "ariba": "SAP Ariba, the supplier portal where some customers publish tenders. The app reads new events from it.",
    "zoho": "Zoho Books, the accounts system. Approved quotes become estimates there. The app only reads it.",
    "proposals list": "The SharePoint list every presales task lives in. The app mirrors it and does not write to it.",
    "quote request": "A quote being prepared in the app: lines, costs, margin and terms, before it goes to Zoho.",
    "costing report": "The PDF that shows each line's cost, margin and selling price — attached when a quote is sent for approval.",
    "landed cost": "What an item costs once it is here: supplier price plus freight, duty, insurance and other charges.",
    "margin": "Profit as a share of the selling price. A 20% margin on a cost of 80 gives a price of 100.",
    "markup": "Profit as a share of the cost. Not what this app uses — it works in margin.",
    "incoterms": "Who pays for and carries the risk of shipping — EXW, FOB, CIF, DDP and so on.",
    "lead time": "How long a supplier takes to deliver after the order.",
    "validity": "How long a price offer stays open.",
    "enquiry analysis": "The app's read of a task's documents: the items asked for, checked against our past quotes and Zoho.",
    "supplier quote": "A supplier's price offer to us, read into lines so it can be compared and reused.",
    "effective status": "The task's status as the app reads it, after taking the dates and the order fields into account.",
    "follow-up": "The app asks the holder of a late task why it is late; managers see the answers.",
    "status reminder": "A reminder sent before a task's deadline, asking where it stands.",
    "presale": "The team that prepares quotes and bids before an order is won.",
    "end user": "The organisation that will use the goods — sometimes not the one buying them.",
}

_SYSTEM: Final = (
    "You are {agent}, a helper inside Hamdaz's work app (a trading and supply company in the UAE). "
    '{person} selected part of the screen "{page}" and wants to know what it is or what it means.\n\n'
    "Explain plainly and briefly, in two to four sentences, for someone who works here. It is part of a "
    "screen in this app, so read its labels and numbers and say what they show and what they are most "
    "likely for — a dashboard counter, a table, a form, a status. Answer the question asked. Use the "
    "glossary below where it applies. Do not invent figures, names, dates or reasons that are not on the "
    "screen; when the question needs something the screen does not show, say that part is not shown.\n"
    "{glossary}"
    'Reply with one JSON object, {{"answer": "..."}}, and nothing else.'
)

SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def from_glossary(text: str) -> str | None:
    """The glossary's answer when the selection is one of its words, else None."""
    key = re.sub(r"[^a-z0-9 /-]+", "", text.strip().lower()).strip()
    if not key or len(key) > 40:
        return None
    if key in GLOSSARY:
        return GLOSSARY[key]
    singular = key[:-1] if key.endswith("s") else key
    return GLOSSARY.get(singular)


def relevant_glossary(*texts: str) -> str:
    """Only the glossary entries whose words appear in what is being asked about."""
    haystack = " ".join(texts).lower()
    hits = [f"- {k}: {v}" for k, v in GLOSSARY.items() if re.search(rf"\b{re.escape(k)}s?\b", haystack)]
    return "\nGlossary:\n" + "\n".join(hits) + "\n" if hits else ""


async def explain(
    model: AgentModel,
    free: TextModel,
    *,
    agent: str,
    person: str,
    page: str,
    text: str,
    question: str,
    history: list[dict[str, str]],
    user_key: str,
    spent: Spent,
    paid_fallback: bool,
) -> tuple[str, str, str | None]:
    """An answer, how it was found (``glossary``, ``free``, ``ai``, ``busy``), and which model."""
    if not question and not history:
        known = from_glossary(text)
        if known:
            return known, "glossary", None
    glossary = relevant_glossary(text, question, *(str(t.get("text") or "") for t in history))
    system = _SYSTEM.format(agent=agent, person=person, page=page or "this page", glossary=glossary)
    turns = [f'Selected on the screen:\n"""\n{text[:6000]}\n"""\n\nThey said: What is this?']
    for turn in history[-10:]:
        who = "You said" if turn.get("role") == "agent" else "They said"
        turns.append(f"{who}: {str(turn.get('text') or '')[:2000]}")
    if question:
        turns.append(f"They said: {question[:2000]}")
    transcript = "\n\n".join(turns)

    answer = await free.ask(
        # Low reasoning: a chat answer in about a second rather than three or four.
        system=system, user=transcript, max_tokens=1200, free_only=True, label="companion explain",
        reasoning_effort="low",
        check=lambda p: isinstance(p.get("answer"), str) and bool(p["answer"].strip()),
    )
    if answer is not None:
        return str(answer.payload["answer"]).strip(), "free", answer.who
    if not paid_fallback or not model.configured:
        return "The free models are busy right now — try again in a minute.", "busy", None
    payload = await model.ask(
        system=system, content=[{"type": "text", "text": transcript}], schema=SCHEMA,
        user_key=user_key, spent=spent, max_tokens=1200,
    )
    return str(payload.get("answer") or "").strip() or "I can't tell from what is selected.", "ai", model.model
