"""Mail watch, step 3: is this tender already on the Proposals list, and whose is it.

Searched in the app's copy of the SharePoint list (``proposal_index``, kept
current by the Proposals sync), and the row it lands on is then **read live
from SharePoint** so the assignee and status shown are the list's own, not the
copy's. Read only.

The funnel, cheapest and surest first:

1. **An earlier mail** about the same Ariba event (same ``Doc`` number) or in
   the same Outlook thread, already matched — the same task.
2. **The tender number.** Proposals titles nearly always start with it
   (``6000153446 RFQ 6000153446 - BELTS and PULLIES``), so a number found in a
   row's title, notes or quote number is the task. PR numbers are tried only
   when no tender number is in the mail.
3. **The words of the title**, for rows without a number — an EOI typed by
   hand, "ENQUIRY - …". Stage words, numbers and filler are taken out of both
   sides first. A row carrying a *different* tender number of the same kind is
   marked down hard: ``6000153548 BELTS and PULLIES`` is not
   ``6000153446 BELTS and PULLIES``, however alike the words.
4. **Haiku**, only for the close calls the words could not settle, choosing
   among the best five or none.

The verdict for a new tender: **new**, **existing**, **stage change** (the
task is at an earlier stage — EOI then RFQ), **duplicate mail** (no task yet,
but an earlier mail brought the same tender), or **unsure**. For a
negotiation: **existing** with the task's details, **not found**, or unsure.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Final

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.model import AgentModel, Spent
from app.enquiries.claude import LLMError
from app.mailwatch.classify import NEGOTIATION, NEW_TENDER, stage_of
from app.models.mailwatch import WatchedMail
from app.models.proposal_index import ProposalIndexItem
from app.proposals.mirror import to_task
from app.proposals.sharepoint import ProposalTask, SharePointProposals

logger = logging.getLogger("hamdaz.mailwatch.match")

NEW: Final = "new"
EXISTING: Final = "existing"
STAGE_CHANGE: Final = "stage_change"
DUPLICATE_MAIL: Final = "duplicate_mail"
NOT_FOUND: Final = "not_found"
UNSURE: Final = "unsure"

#: Title scores: at or above SURE it is the task; below LIKELY it is not.
SURE: Final = 0.85
LIKELY: Final = 0.5
#: The best must lead the next by this much to be taken without asking…
LEAD: Final = 0.1
#: …and no other row may score this well: two good rows are usually one
#: requirement at two stages (the EOI and the RFT), and the newer is wanted.
RIVAL: Final = 0.65

#: Earlier stages first. A task at a lower rank than the mail is a stage change.
STAGE_RANK: Final = {
    "pre_tender": 0, "eoi": 1, "rfi": 1, "rfq": 2, "rfp": 2, "rft": 2, "negotiation": 3,
}

_EVENT: Final = re.compile(r"^[36]\d{9}$")


@dataclass
class Candidate:
    item_id: str
    title: str
    customer: str | None
    assignee: str | None
    status: str | None
    created_at: str | None
    score: float
    why: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Match:
    verdict: str
    #: earlier_mail, reference, title, model — how the task was found.
    method: str | None = None
    item_id: str | None = None
    score: float = 0.0
    reason: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    #: The task as SharePoint has it now — see ``task_details``.
    task: dict[str, Any] | None = None
    duplicate_of_id: uuid.UUID | None = None
    model_cost_usd: float = 0.0
    error: str | None = None


# ── words ───────────────────────────────────────────────────────────

_STOP: Final = frozenset(
    """
    a an and the of for to in on at by with from or as is be this that our your
    fw fwd re rfq rfp rft rfi eoi itt itb tender tenders request quotation quote
    proposal expression interest single stage two event events negotiation nego
    pr no number ref reference supply procurement provision adnoc abu dhabi
    national oil company urgent top requirement new doc invited participate
    """.split()
)


def words(text_value: str | None) -> list[str]:
    """The meaningful words: lowercase, no numbers, no stage or filler words,
    no two-letter Ariba area codes (DB4, NC3 are dropped with the digits)."""
    raw = re.findall(r"[a-z][a-z]+", (text_value or "").lower())
    return [w for w in raw if w not in _STOP and len(w) > 2]


def numbers_in(text_value: str | None) -> set[str]:
    return set(re.findall(r"(?<!\d)(\d{6,10})(?!\d)", text_value or ""))


def _customer_key(value: str | None) -> str:
    v = (value or "").lower()
    if "adnoc" in v or "abu dhabi national oil" in v:
        return "adnoc"
    return re.sub(r"[^a-z]", "", v)[:20]


def title_score(query: str, row_title: str) -> float:
    """How alike two titles are, 0 to 1, on their meaningful words."""
    a, b = set(words(query)), set(words(row_title))
    if not a or not b:
        return 0.0
    shared = len(a & b)
    containment = shared / min(len(a), len(b))
    jaccard = shared / len(a | b)
    ratio = SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()
    return round(0.5 * containment + 0.25 * jaccard + 0.25 * ratio, 3)


# ── the funnel ──────────────────────────────────────────────────────


def _refs(references: list[str]) -> tuple[list[str], list[str]]:
    """Tender numbers (event and portal), then PR numbers. POs are an order's,
    not a tender's, and are not matched on."""
    tender, pr = [], []
    for ref in references:
        digits = re.sub(r"\D", "", ref)
        if not digits:
            continue
        if ref.startswith("PR"):
            pr.append(digits)
        elif ref.startswith("PO"):
            continue
        else:
            tender.append(digits)
    return list(dict.fromkeys(tender)), list(dict.fromkeys(pr))


def _live_rows():
    return select(ProposalIndexItem).where(ProposalIndexItem.deleted.is_(False))


async def by_number(session: AsyncSession, numbers: list[str]) -> list[ProposalIndexItem]:
    """Rows carrying any of these numbers as a whole number, newest first."""
    if not numbers:
        return []
    conditions = []
    for n in numbers:
        pattern = f"(^|[^0-9]){n}([^0-9]|$)"
        conditions += [
            ProposalIndexItem.title.op("~")(pattern),
            ProposalIndexItem.quote_no.op("~")(pattern),
            ProposalIndexItem.remarks.op("~")(pattern),
            ProposalIndexItem.working_notes.op("~")(pattern),
        ]
    rows = await session.scalars(
        _live_rows().where(or_(*conditions))
        .order_by(ProposalIndexItem.sp_created_at.desc().nulls_last()).limit(10)
    )
    return list(rows.all())


async def by_words(
    session: AsyncSession, query: str, *, customer: str | None, numbers: set[str]
) -> list[Candidate]:
    """The rows whose titles are most like ``query``, scored, best first."""
    tokens = list(dict.fromkeys(words(query)))
    if len(tokens) < 2:
        return []
    tsquery = func.to_tsquery(text("'english'::regconfig"), " | ".join(tokens))
    rows = (
        await session.scalars(
            _live_rows()
            .where(ProposalIndexItem.search_vector.op("@@")(tsquery))
            .order_by(func.ts_rank(ProposalIndexItem.search_vector, tsquery).desc())
            .limit(30)
        )
    ).all()
    want = _customer_key(customer)
    out: list[Candidate] = []
    for row in rows:
        score = title_score(query, row.title)
        why = [f"words {score:.2f}"]
        if want and want == _customer_key(row.end_user):
            score += 0.05
            why.append("same customer")
        theirs = {n for n in numbers_in(row.title) if _EVENT.match(n)}
        ours = {n for n in numbers if _EVENT.match(n)}
        if ours and theirs and not ours & theirs and {n[0] for n in ours} & {n[0] for n in theirs}:
            # Another tender of the same kind with near-identical words.
            score *= 0.5
            why.append("different tender number")
        out.append(_candidate(row, min(score, 1.0), ", ".join(why)))
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:5]


def _candidate(row: ProposalIndexItem, score: float, why: str) -> Candidate:
    return Candidate(
        item_id=row.item_id,
        title=row.title.strip(),
        customer=row.end_user,
        assignee=row.assigned_name,
        status=row.effective_status or row.status,
        created_at=row.sp_created_at.isoformat() if row.sp_created_at else None,
        score=round(score, 3),
        why=why,
    )


async def earlier_mail(
    session: AsyncSession,
    *,
    ariba_doc: str | None,
    conversation_id: str | None,
    tender_numbers: list[str],
    exclude_id: uuid.UUID | None,
    before: datetime | None,
) -> WatchedMail | None:
    """The first kept mail about the same event: same Ariba document, same
    thread, or the same tender number."""
    conditions = []
    if ariba_doc:
        conditions.append(WatchedMail.ariba_doc == ariba_doc)
    if conversation_id:
        conditions.append(WatchedMail.conversation_id == conversation_id)
    if tender_numbers:
        conditions.append(WatchedMail.reference_numbers.overlap(tender_numbers))
    if not conditions:
        return None
    query = select(WatchedMail).where(
        or_(*conditions), WatchedMail.category.in_((NEW_TENDER, NEGOTIATION))
    )
    if exclude_id is not None:
        query = query.where(WatchedMail.id != exclude_id)
    if before is not None:
        query = query.where(WatchedMail.received_at < before)
    return await session.scalar(query.order_by(WatchedMail.received_at.asc()).limit(1))


# ── the model, for close calls ──────────────────────────────────────

PICK_SYSTEM: Final = """You decide whether an email about a tender refers to
one of the existing tasks on a presales team's list. Tenders are often renamed
between stages (an EOI becomes an RFQ or RFT with a new number), and different
tenders can have near-identical titles (two separate RFQs for "belts and
pulleys"). Pick a task only when it is clearly the same requirement from the
same customer; otherwise answer -1. When several tasks are the same
requirement at different stages, pick the most recently created one."""

PICK_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["choice", "confidence", "reason"],
    "properties": {
        "choice": {"type": "integer", "description": "The task's number in the list, or -1 for none."},
        "confidence": {"type": "number"},
        "reason": {"type": "string", "description": "Why, in under 15 words."},
    },
}


async def model_pick(
    model: AgentModel, *, mail: str, candidates: list[Candidate]
) -> tuple[Candidate | None, float, str, float]:
    spent = Spent()
    listing = "\n".join(
        f"{i}. {c.title} | customer: {c.customer or '-'} | status: {c.status or '-'} | "
        f"created: {(c.created_at or '')[:10]}"
        for i, c in enumerate(candidates)
    )
    answer = await model.ask(
        system=PICK_SYSTEM,
        content=[{"type": "text", "text": f"Email:\n{mail}\n\nTasks:\n{listing}"}],
        schema=PICK_SCHEMA, user_key="mailwatch", spent=spent, max_tokens=600,
    )
    cost = float(spent.cost(model.model))
    choice = int(answer.get("choice", -1))
    confidence = max(0.0, min(1.0, float(answer.get("confidence") or 0)))
    reason = str(answer.get("reason") or "")[:300]
    if 0 <= choice < len(candidates):
        return candidates[choice], confidence, reason, cost
    return None, confidence, reason, cost


# ── live from SharePoint ────────────────────────────────────────────


def task_details(task: ProposalTask, *, email: str | None, live: bool) -> dict[str, Any]:
    """What a person needs to know about the task, as SharePoint has it."""
    return {
        "item_id": task.id,
        "title": (task.title or "").strip(),
        "assignee": task.assigned_to_name,
        "assignee_email": email,
        "status": task.effective_status,
        "submission_status": task.submission_status,
        "current_type": task.current_type,
        "stage": task_stage(task.title, task.current_type),
        "customer": task.end_user,
        "quote_no": task.quote_no,
        "bid_closing_date": task.bid_closing_date,
        "due_date": task.due_date,
        "negotiation": task.negotiation,
        "order_status": task.order_status,
        "remarks": task.remarks,
        "created_at": task.created_at,
        "url": task.web_url,
        #: False when SharePoint could not be reached and the copy answered.
        "live": live,
    }


def task_stage(title: str | None, current_type: str | None) -> str | None:
    """The stage a task is at: its title says it best ("6000153644 EOI RFQ …"
    is the EOI). The CurrentType column is RFP on nearly every row, so it is
    used only when the title names nothing."""
    found = stage_of(title or "")
    if found:
        return found
    kind = (current_type or "").strip().lower()
    return kind if kind in STAGE_RANK else None


async def read_task(
    session: AsyncSession, sharepoint: SharePointProposals | None, item_id: str
) -> dict[str, Any] | None:
    row = await session.get(ProposalIndexItem, item_id)
    email = row.assigned_email if row else None
    if sharepoint is not None:
        try:
            return task_details(await sharepoint.task(item_id), email=email, live=True)
        except Exception as exc:  # noqa: BLE001 - the copy answers instead
            logger.warning("mail watch could not read task %s live: %s", item_id, exc)
    if row is None:
        return None
    return task_details(to_task(row), email=email, live=False)


# ── the whole of it ─────────────────────────────────────────────────


async def match(
    session: AsyncSession,
    *,
    category: str | None,
    stage: str | None,
    title: str | None,
    subject: str | None,
    customer: str | None,
    references: list[str],
    ariba_doc: str | None,
    conversation_id: str | None,
    received_at: datetime | None,
    mail_id: uuid.UUID | None,
    sharepoint: SharePointProposals | None,
    model: AgentModel | None,
    use_model: bool,
) -> Match | None:
    """The verdict for one mail, or None for a category that is not matched."""
    if category not in (NEW_TENDER, NEGOTIATION):
        return None
    tender_numbers, pr_numbers = _refs(references)

    earlier = await earlier_mail(
        session, ariba_doc=ariba_doc, conversation_id=conversation_id,
        tender_numbers=tender_numbers, exclude_id=mail_id, before=received_at,
    )
    found: Match | None = None
    if earlier is not None and earlier.matched_item_id:
        found = Match(
            verdict=EXISTING, method="earlier_mail", item_id=earlier.matched_item_id, score=1.0,
            reason="an earlier mail about the same event was matched to it",
        )

    if found is None:
        rows = await by_number(session, tender_numbers) or await by_number(session, pr_numbers)
        if rows:
            which = "tender number" if tender_numbers else "PR number"
            candidates = [_candidate(r, 1.0, f"same {which}") for r in rows[:5]]
            found = Match(
                verdict=EXISTING, method="reference", item_id=rows[0].item_id, score=1.0,
                reason=f"same {which} in the task" + (
                    f" ({len(rows)} tasks carry it; the newest is taken)" if len(rows) > 1 else ""
                ),
                candidates=candidates,
            )

    if found is None:
        query = title or subject or ""
        candidates = await by_words(
            session, query, customer=customer, numbers=set(tender_numbers)
        )
        best = candidates[0] if candidates else None
        second = candidates[1].score if len(candidates) > 1 else 0.0
        if best and best.score >= SURE and best.score - second >= LEAD and second < RIVAL:
            found = Match(
                verdict=EXISTING, method="title", item_id=best.item_id, score=best.score,
                reason=f"title alike ({best.why})", candidates=candidates,
            )
        elif best and best.score >= LIKELY:
            found = Match(
                verdict=UNSURE, method="title", score=best.score, candidates=candidates,
                reason="titles alike but not certainly the same task",
            )
            if use_model and model is not None and model.configured:
                mail_text = (
                    f"Subject: {subject or ''}\nTask title read from it: {title or ''}\n"
                    f"Customer: {customer or ''}\nReferences: {', '.join(references) or 'none'}"
                )
                try:
                    pick, confidence, reason, cost = await model_pick(
                        model, mail=mail_text, candidates=candidates
                    )
                    found.model_cost_usd = cost
                    if pick is not None and confidence >= 0.7:
                        found.verdict, found.method = EXISTING, "model"
                        found.item_id, found.score = pick.item_id, confidence
                        found.reason = f"Haiku: {reason}"
                    elif pick is None and confidence >= 0.7:
                        found.verdict = NEW
                        found.reason = f"Haiku: none of these — {reason}"
                    else:
                        found.reason = f"Haiku unsure: {reason}"
                except LLMError as exc:
                    found.error = f"model: {exc}"[:500]
        else:
            found = Match(verdict=NEW, candidates=candidates, reason="no task with this number or title")

    # What the task says now, from SharePoint itself.
    if found.item_id:
        found.task = await read_task(session, sharepoint, found.item_id)
        if found.task is None:
            found.verdict, found.item_id = NEW, None
            found.reason = "the matched task is no longer on the list"

    if category == NEGOTIATION:
        if found.verdict == NEW:
            found.verdict = NOT_FOUND
        return found

    if found.verdict == EXISTING and found.task:
        theirs = found.task.get("stage")
        if stage and theirs and STAGE_RANK.get(stage, -1) > STAGE_RANK.get(theirs, 99):
            found.verdict = STAGE_CHANGE
            found.reason += f"; the task is at {theirs.upper()}, this mail is {stage.upper()}"
    if found.verdict == NEW and earlier is not None:
        found.verdict = DUPLICATE_MAIL
        found.duplicate_of_id = earlier.id
        found.reason = "no task yet, but an earlier mail brought the same tender"
    elif earlier is not None:
        found.duplicate_of_id = earlier.id
    return found
