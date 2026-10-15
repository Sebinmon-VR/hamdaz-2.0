"""The companion's help: answer a question, or walk the person through a task.

One door (``POST /agents/assist``) for anything typed into the companion's
bubble, and one for each step of a walk-through (``POST /agents/guide/next``).
The rule is the same as everywhere in agents: the app answers what it can,
and a free model (Groq, through ``TextModel``) is asked only for what is left.

**What the app answers itself**

* A word from the glossary — free, instant.
* "Convert this to AED": the figure, its currency (from the box, its column,
  or the one currency the screen shows), and Zoho Books' own rate — the rate
  quotes are priced at. Arithmetic is never left to a model.
* "Take me to leave": a page whose name is in the question.
* The common jobs (raise a quote, compare quotes, request leave, file a
  report, make an agent, add a line): a recipe of which page, then which
  button, matched against the buttons actually on the screen.

**What the free model is asked**

* To explain a part of the screen, given the text in the box *and* what is
  around it — its column, its row, its section, the currencies on show — so
  "111.64" arrives as "COST of 16A Plug, in USD", not as a bare number.
* The next step of a walk-through when no recipe covers it: it is handed the
  goal, the steps done, the page's own buttons and fields (each with an id),
  and the pages the person can open, and must pick one of those ids or paths.
  Anything else it answers is thrown away, so it cannot send anybody to a
  button that does not exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

from app.agents.explain import GLOSSARY, from_glossary, relevant_glossary
from app.agents.model import AgentModel, Spent
from app.core.llm import TextModel
from app.quoting.fx import FxUnavailableError, zoho_rate

CURRENCIES: Final = ("AED", "USD", "EUR", "GBP", "SAR", "QAR", "OMR", "KWD", "BHD", "INR", "CNY", "JPY", "CHF", "SGD")
_CODE = re.compile(r"\b(" + "|".join(CURRENCIES) + r")\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})*(?:\.\d+)?(?![\w])|(?<![\w.])-?\d+(?:\.\d+)?(?![\w])")
_CONVERT = re.compile(
    r"\b(?:convert|conver|covert|change|exchange|how much is|what is|in)\b.*?\b(?:to|into|in)\s+(" + "|".join(CURRENCIES) + r")\b"
    r"|\b(?:to|into|in)\s+(" + "|".join(CURRENCIES) + r")\b\s*\??$",
    re.I,
)
#: "How do I…", "take me to…", "where is…": a walk-through, not an answer.
_GUIDE = re.compile(
    r"\b(how (?:do|can|to|would|should) (?:i|we|you)?|how to|where (?:is|are|do|can)|take me|go to|open (?:the )?|"
    r"show me (?:how|where)|walk me|guide me|help me (?:to )?(?:create|raise|make|add|request|file|apply|start|send|find)|"
    r"steps? (?:to|for)|i want to|i need to)\b",
    re.I,
)
#: A US-dollar peg, used only when Zoho cannot be read — and said so.
USD_AED_PEG: Final = Decimal("3.6725")

MAX_GUIDE_STEPS: Final = 12


# ── what the browser sends ─────────────────────────────────────────────


@dataclass(slots=True)
class Around:
    """What surrounds a boxed part of the screen."""

    column: str = ""
    row: str = ""
    section: str = ""
    currencies: tuple[str, ...] = ()
    #: The visible page, condensed: its headings, labels and figures, in order.
    page_text: str = ""

    def describe(self) -> str:
        parts = []
        if self.section:
            parts.append(f"Section: {self.section}")
        if self.column:
            parts.append(f"Column: {self.column}")
        if self.row:
            parts.append(f"Row: {self.row}")
        if self.currencies:
            parts.append(f"Currencies shown on the screen: {', '.join(self.currencies)}")
        return "\n".join(parts)


# ── converting a figure ────────────────────────────────────────────────


def _amount(text: str) -> Decimal | None:
    match = _NUMBER.search(text or "")
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def conversion_target(question: str) -> str | None:
    match = _CONVERT.search(question or "")
    if not match:
        return None
    return (match.group(1) or match.group(2) or "").upper() or None


async def convert(zoho: Any, *, question: str, selection: str, around: Around) -> tuple[str, str] | None:
    """A figure in another currency, worked out by the app. None when it is not a conversion."""
    target = conversion_target(question)
    if not target:
        return None
    amount = _amount(question) or _amount(selection)
    if amount is None:
        return f"Which figure should I convert to {target}? Draw the box around it, or type it.", "system"
    codes = [c.upper() for c in _CODE.findall(f"{question} {selection}") if c.upper() != target]
    if not codes:
        codes = [c.upper() for c in _CODE.findall(f"{around.column} {around.row}") if c.upper() != target]
    if not codes:
        others = [c for c in around.currencies if c != target]
        codes = others if len(set(others)) == 1 else []
    if not codes:
        return f"I can see {amount:,} but not which currency it is in. Which one is it?", "system"
    source = codes[0]
    if source == target:
        return f"{amount:,} is already in {target}.", "system"

    try:
        if zoho is None or not getattr(zoho, "configured", False):
            raise FxUnavailableError("Zoho Books is not connected.")
        fx = await zoho_rate(zoho, quote_currency=source, supplier_currency=target)
        rate, where = fx.rate, f"Zoho Books' rate{f' of {fx.effective_date:%d %b %Y}' if fx.effective_date else ''}"
    except Exception:  # noqa: BLE001 — Zoho down or no rate: the peg, or say so
        if {source, target} == {"USD", "AED"}:
            rate = USD_AED_PEG if source == "USD" else (Decimal(1) / USD_AED_PEG)
            where = "the fixed USD–AED peg (Zoho Books could not be read)"
        else:
            return f"I could not read an exchange rate for {source} to {target} from Zoho Books just now.", "system"
    converted = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    shown = rate.normalize() if rate == rate.to_integral() else rate.quantize(Decimal("0.000001")).normalize()
    return (
        f"{amount:,} {source} = {converted:,} {target}, at 1 {source} = {shown} {target} — {where}.",
        "system",
    )


# ── answering ──────────────────────────────────────────────────────────

#: What the app is, so the model reads a screen as somebody who works here
#: would. Short on purpose: it rides on every question.
APP_BRIEFING: Final = """\
Hamdaz is a UAE trading and supply company (oil & gas, utilities, government buyers). This app runs its presales and office work:
- Proposals / My tasks: every presales task (an RFQ or tender) from the SharePoint Proposals list, with its BCD (bid closing date) and status.
- Enquiries: the app reads a task's documents into the items the customer wants, checked against our past quotes and Zoho.
- Comparisons: supplier quotations read into lines and compared per unit.
- Quote requests: a quote being priced. Each line has a cost (what the supplier charges, in the supplier's currency), a landed cost (cost plus freight, duty, insurance, bank charges), a margin and a selling price. SP = landed cost / (1 - margin%). Gross profit = SP - landed cost; gross margin = profit / price. Tabs: Supplier quotes, Quote & lines, Costing report, Landed cost, Summary, Compliance matrix, Costing sheet, Portal fields, Discussion. An approved quote goes to Zoho Books as an estimate.
- Quotes: quotes already in Zoho Books. Tenders: events read from SAP Ariba.
- Leave, Reports (daily/weekly work reports per team), Projects, Teams, Directory, Notifications, Follow-ups (why a task is late), Status reminders.
- Agents: each person's own AI agents, and this companion."""

_ANSWER_SYSTEM: Final = (
    "You are {agent}, a helper inside Hamdaz's work app. {person} is on the screen \"{page}\".\n\n"
    "{briefing}\n\n"
    "Think like a colleague who knows this app. Use the screen's own text — the part they boxed, what is "
    "around it, and the rest of the visible page — to work out what something is, why it shows what it "
    "shows, and what they can do about it. Connect the pieces: a figure's currency, unit or meaning is "
    "often stated elsewhere on the page (a total, a header, a toggle); use it, and say where you saw it. "
    "Work things out from the figures shown when it helps, but NEVER do the arithmetic yourself: write "
    "each calculation as [[expression]] using only numbers and + - * / ( ), for example [[2005.05/(1-0.25)]], "
    "and the app puts the exact result in its place. Answer the question they asked, in two to five "
    "short sentences, plainly. Do not invent figures, names, dates or reasons that are on neither the "
    "screen nor the briefing; if the answer truly is not there, say what is missing and where they could "
    "find it in the app.\n"
    "{glossary}"
    'Reply with one JSON object, {{"answer": "..."}}, and nothing else.'
)

ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


_SUM = re.compile(r"\[\[([^\[\]]{1,200})\]\]")


def _evaluate(expression: str) -> Decimal:
    """+ - * / and brackets over plain numbers, in Decimal. Anything else is refused."""
    import ast

    tree = ast.parse(expression.replace(",", "").replace("×", "*").replace("÷", "/").strip(), mode="eval")

    def walk(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return Decimal(str(node.value))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right
        raise ValueError("not plain arithmetic")

    return walk(tree)


def work_out_sums(text: str) -> str:
    """Every [[expression]] the model wrote, replaced by its exact result.

    The model reasons about which sum to do; the app does it. A sum it
    cannot read is left out rather than guessed at.
    """

    def put(match: re.Match[str]) -> str:
        try:
            value = _evaluate(match.group(1))
        except (ValueError, SyntaxError, ZeroDivisionError, InvalidOperation, RecursionError):
            return "(could not work that out)"
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{rounded:,.2f}" if rounded != rounded.to_integral() else f"{rounded:,.0f}"

    return _SUM.sub(put, text)


async def answer(
    model: AgentModel,
    free: TextModel,
    *,
    agent: str,
    person: str,
    page: str,
    selection: str,
    around: Around,
    question: str,
    history: list[dict[str, str]],
    user_key: str,
    spent: Spent,
    paid_fallback: bool,
) -> tuple[str, str, str | None]:
    """(answer, how, model). ``how`` is glossary, free, ai or busy."""
    if selection and not question and not history:
        known = from_glossary(selection)
        if known:
            return known, "glossary", None
    if not selection and question:
        known = from_glossary(re.sub(r"^(what('?s| is| does)|define|meaning of)\s+", "", question.strip(" ?"), flags=re.I))
        if known:
            return known, "glossary", None

    glossary = relevant_glossary(selection, question, *(str(t.get("text") or "") for t in history))
    system = _ANSWER_SYSTEM.format(
        agent=agent, person=person, page=page or "this page", glossary=glossary, briefing=APP_BRIEFING
    )
    turns: list[str] = []
    if around.page_text:
        turns.append(f'The visible page, condensed:\n"""\n{around.page_text[:5000]}\n"""')
    if selection:
        context = around.describe()
        turns.append(
            f'They boxed this part of the screen:\n"""\n{selection[:6000]}\n"""'
            + (f"\n\nAround it:\n{context}" if context else "")
        )
        if not question and not history:
            turns.append("They said: What is this?")
    for turn in history[-10:]:
        who = "You said" if turn.get("role") == "agent" else "They said"
        turns.append(f"{who}: {str(turn.get('text') or '')[:2000]}")
    if question:
        turns.append(f"They said: {question[:2000]}")
    transcript = "\n\n".join(turns) or "They said: Hello"

    found = await free.ask(
        # Medium, not low: low answered fast and shallow. Guide steps stay low.
        system=system, user=transcript, max_tokens=2500, free_only=True, label="companion answer",
        reasoning_effort="medium",
        check=lambda p: isinstance(p.get("answer"), str) and bool(p["answer"].strip()),
    )
    if found is not None:
        return work_out_sums(str(found.payload["answer"]).strip()), "free", found.who
    if not paid_fallback or not model.configured:
        return "The free models are busy right now — try again in a minute.", "busy", None
    payload = await model.ask(
        system=system, content=[{"type": "text", "text": transcript}], schema=ANSWER_SCHEMA,
        user_key=user_key, spent=spent, max_tokens=1200,
    )
    said = work_out_sums(str(payload.get("answer") or "").strip())
    return said or "I can't tell from what is shown.", "ai", model.model


def wants_guide(question: str) -> bool:
    return bool(question and _GUIDE.search(question)) and conversion_target(question) is None


# ── walking somebody through a job ─────────────────────────────────────


@dataclass(slots=True)
class Element:
    id: str
    label: str
    kind: str


@dataclass(slots=True)
class Page:
    path: str
    label: str


@dataclass(slots=True)
class Step:
    """What to do next. ``action``: click, navigate, done, or say (nothing to point at)."""

    action: str
    say: str
    target: str | None = None
    path: str | None = None
    how: str = "system"


#: The jobs people ask for most, as the app knows them: which page, then
#: which button on it. Labels are matched loosely ("new quote" finds "New
#: quote request"); a step whose button is not on the screen is left to the
#: model. ``None`` as a page means "wherever you are".
RECIPES: Final[list[tuple[re.Pattern[str], list[tuple[str | None, tuple[str, ...], str]]]]] = [
    (
        re.compile(r"quote request|(raise|make|create|prepare|start|new|do) (a |the )?(new )?quot(e|ation)", re.I),
        [
            ("/quote-requests", ("new quote",), "Start here: press New quote."),
            ("/quote-requests/new", ("start a blank quote", "blank quote"), "Start from a task's enquiry, or press this for a blank quote."),
        ],
    ),
    (
        re.compile(r"compar(e|ison)", re.I),
        [("/comparisons", ("new comparison",), "Press New comparison, then drop the supplier quotes in.")],
    ),
    (
        re.compile(r"\bleave\b|day off|days off|holiday|vacation|sick", re.I),
        [
            ("/leave", ("request leave",), "Press Request leave."),
            ("/leave/request", (), "Fill in the dates and the type, then send it."),
        ],
    ),
    (
        re.compile(r"\breport\b", re.I),
        [("/reports", ("file a report", "new report", "file"), "Press File a report.")],
    ),
    (
        re.compile(r"\bagent\b", re.I),
        [("/agents", ("new agent",), "Press New agent to make one.")],
    ),
    (
        re.compile(r"add (a |another |new )?(line|item|row)", re.I),
        [(None, ("add line", "add item"), "Press Add line.")],
    ),
    (
        re.compile(r"enquir|analy[sz]e (a |the |my )?(task|tender|rfq)", re.I),
        [("/enquiries", (), "Your analysed enquiries are here; open a task from My tasks to analyse a new one.")],
    ),
]

_GUIDE_SYSTEM: Final = (
    "You guide {person} through Hamdaz's work app, one step at a time. Goal: \"{goal}\".\n"
    "They are on the page \"{page}\" ({path}).\n\n"
    "Choose the ONE next thing they should do. You may only point at an element from ELEMENTS (by its id) "
    "or go to a page from PAGES (by its path). If the goal is already reached, or nothing listed helps, use "
    "action done and say so plainly. Never repeat a step already done. Keep \"say\" to one short sentence "
    "telling them what to do and why.\n"
    'Reply with one JSON object: {{"action": "click"|"navigate"|"done", "target": "<element id or empty>", '
    '"path": "<page path or empty>", "say": "..."}} and nothing else.'
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _find(elements: list[Element], wanted: tuple[str, ...]) -> Element | None:
    for want in wanted:
        w = _norm(want)
        exact = next((e for e in elements if _norm(e.label) == w), None)
        if exact:
            return exact
        loose = next((e for e in elements if w and w in _norm(e.label)), None)
        if loose:
            return loose
    return None


def page_named(goal: str, pages: list[Page]) -> Page | None:
    """The page a "take me to …" names, longest name first so "leave requests" beats "leave"."""
    text = _norm(goal)
    for page in sorted(pages, key=lambda p: -len(p.label)):
        label = _norm(page.label)
        if label and re.search(rf"\b{re.escape(label)}\b", text):
            return page
    return None


def by_recipe(goal: str, path: str, elements: list[Element], pages: list[Page], done: list[str]) -> Step | None:
    """The next step from a recipe, when one fits the goal and the screen."""
    for pattern, steps in RECIPES:
        if not pattern.search(goal):
            continue
        known = {p.path for p in pages}
        for index, (where, labels, say) in enumerate(steps):
            if where is not None and where != path:
                continue
            if not labels:
                return Step("done", say)
            element = _find(elements, labels)
            if element is not None and element.id not in done:
                return Step("click", say, target=element.id)
        # Not on any of the recipe's pages yet: go to its first one.
        first = next((w for w, _, _ in steps if w), None)
        if first and first != path and first in known and f"nav:{first}" not in done:
            return Step("navigate", f"Let's go to {next(p.label for p in pages if p.path == first)}.", path=first)
        return None
    return None


async def next_step(
    free: TextModel,
    *,
    person: str,
    goal: str,
    page_title: str,
    path: str,
    elements: list[Element],
    pages: list[Page],
    done: list[str],
) -> Step:
    """The next thing to do: the app's own answer if it has one, else the free model's — checked."""
    if len(done) >= MAX_GUIDE_STEPS:
        return Step("done", "That's as far as I can take you — carry on from here, or ask me again.")

    # "Take me to …": a page named in the goal, and we are not on it.
    named = page_named(goal, pages)
    if named and named.path != path and f"nav:{named.path}" not in done and not any(p.search(goal) for p, _ in RECIPES):
        return Step("navigate", f"Here's {named.label}.", path=named.path)
    if named and named.path == path and not any(p.search(goal) for p, _ in RECIPES):
        return Step("done", f"You're on {named.label}.")

    step = by_recipe(goal, path, elements, pages, done)
    if step is not None:
        return step

    listing = "\n".join(f"{e.id}: [{e.kind}] {e.label}" for e in elements[:120]) or "(none)"
    page_list = "\n".join(f"{p.path}: {p.label}" for p in pages[:80])
    system = _GUIDE_SYSTEM.format(person=person, goal=goal[:300], page=page_title or path, path=path)
    user = (
        f"ELEMENTS on this page:\n{listing}\n\nPAGES they can open:\n{page_list}\n\n"
        f"Steps already done: {', '.join(done[-10:]) or 'none'}"
    )
    ids = {e.id for e in elements}
    paths = {p.path for p in pages}

    def valid(p: dict[str, Any]) -> bool:
        action = p.get("action")
        if action == "click":
            return p.get("target") in ids and p.get("target") not in done
        if action == "navigate":
            return p.get("path") in paths and p.get("path") != path
        return action == "done"

    found = await free.ask(
        system=system, user=user, max_tokens=800, free_only=True, label="companion guide",
        reasoning_effort="low", check=valid,
    )
    if found is None:
        return Step("done", "I'm not sure of the next step from here. Try asking in other words.", how="busy")
    p = found.payload
    say = str(p.get("say") or "").strip()[:300] or "This one."
    if p["action"] == "click":
        return Step("click", say, target=str(p["target"]), how="free")
    if p["action"] == "navigate":
        return Step("navigate", say, path=str(p["path"]), how="free")
    return Step("done", say, how="free")


__all__ = ["GLOSSARY", "Around", "Element", "Page", "Step", "answer", "convert", "next_step", "wants_guide"]
