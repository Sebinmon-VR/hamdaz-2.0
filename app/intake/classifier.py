"""Deciding what an email is about, and pulling the details out of it.

**One call, not two.** Classifying and extracting are asked together because
they are one judgement: what a message *is* and what it *says* are decided from
the same words, and two calls can disagree — a message classified as a
negotiation whose extraction found a tender number and no quote is a
contradiction nobody downstream can resolve.

**A refusal is an answer.** The model is told to return ``unknown`` when the
mail does not clearly fit, and the pipeline treats low confidence as "leave it
alone". That matters more here than in most places: acting on a bad guess
creates a real row in the live Proposals list, assigned to a real person, from
an email that was actually a thank-you note.

**No model first, then the free one, then Claude.** Mail that is plainly not
work — an out-of-office, a bounce, a read receipt, a meeting reply — is settled
by its subject and sender alone, and no model is asked. The rest goes through
``app.core.llm.TextModel``: the free tiers in ``LLM_PROVIDERS`` first, and
Claude only when they refuse or run out for the day. The embeddings in the
matcher are OpenAI's because Anthropic does not offer any.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.llm import TextModel, trim
from app.models.intake import MailCategory

logger = logging.getLogger("hamdaz.intake.classify")


class ClassifierError(Exception):
    """The message could not be read. Safe to show a super admin."""


#: The shape the model must answer in. Every field is present in every answer,
#: because a key that is sometimes absent is a key every caller has to guard.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": [c.value for c in MailCategory],
            "description": (
                "tender: an invitation to bid. proposal: a request to quote. "
                "negotiation: a customer coming back on a price or terms for "
                "something already quoted. order: a purchase order against a "
                "quote. general: circulars, acknowledgements, thanks, anything "
                "that needs no action. unknown: it does not clearly fit."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0 to 1. Be honest; low confidence is acted on as 'leave it'.",
        },
        "is_reopened": {
            "type": "boolean",
            "description": (
                "True when the mail says this is coming back rather than "
                "arriving for the first time — reopened, revised, resubmitted, "
                "extended, or a follow-up on something already sent."
            ),
        },
        "title": {
            "type": "string",
            "description": "What the work is, as a task title. Short and specific.",
        },
        "customer": {"type": "string", "description": "The customer or end user."},
        "references": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Every tender number, quote number, RFQ or PO reference in the "
                "mail, exactly as written. These matter more than the wording "
                "for finding the existing task."
            ),
        },
        "deadline": {
            "type": "string",
            "description": "Bid closing or submission date as YYYY-MM-DD, or empty.",
        },
        "summary": {
            "type": "string",
            "description": "Two sentences on what is being asked for.",
        },
        "reasoning": {
            "type": "string",
            "description": "Why this category. One sentence, for somebody auditing it.",
        },
    },
    "required": [
        "category", "confidence", "is_reopened", "title", "customer",
        "references", "deadline", "summary", "reasoning",
    ],
}

_INSTRUCTIONS = """\
You read email that arrives at a trading and contracting company and decide \
what each message is, so the right thing happens to it.

The company bids for tenders, quotes for customers, negotiates prices, and \
receives purchase orders. Most mail is one of those four. A good deal is none \
of them and should be called general.

Rules:
- Judge the message on what it asks for, not on how it is worded. "Kindly \
revert with your best price" is a proposal whether or not the word quote appears.
- is_reopened is about *this* piece of work having been seen before: reopened, \
revised, extended, resubmitted, or chased. A first-time request is not reopened \
however urgent it sounds.
- Copy references exactly as written, including punctuation. Do not normalise \
them, do not invent one, and do not guess at a number that is only implied.
- If the mail is a forward or a reply, judge the newest part. The quoted \
history underneath is context, not the request.
- Return unknown rather than choosing the closest fit. Something acted on here \
becomes a task assigned to a real person; being unsure is the useful answer.
"""


@dataclass(slots=True)
class Classification:
    """What the model made of one message."""

    category: str = MailCategory.UNKNOWN
    confidence: float = 0.0
    is_reopened: bool = False
    title: str = ""
    customer: str = ""
    references: list[str] = field(default_factory=list)
    deadline: str = ""
    summary: str = ""
    reasoning: str = ""
    cost_usd: float = 0.0

    @property
    def creates_work(self) -> bool:
        return self.category in (MailCategory.TENDER, MailCategory.PROPOSAL)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "customer": self.customer,
            "references": list(self.references),
            "deadline": self.deadline,
            "summary": self.summary,
        }

    @property
    def match_text(self) -> str:
        """What the matcher searches the Proposals list with.

        The title and the customer, not the whole email: signature blocks and
        quoted history are noise to a search, and the extraction has already
        decided what the message is actually about.
        """
        parts = [self.title, self.customer, self.summary]
        return "\n".join(p.strip() for p in parts if p and p.strip())


#: Mail no person wrote, recognisable without reading it: the subject a mail
#: server or a calendar puts on it, or the sender that is a mail server.
_AUTOMATIC_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto[- ]?reply|autoreply|out of (the )?office|"
    r"undeliverable|undelivered mail|delivery status notification|"
    r"mail delivery (failed|failure|subsystem)|delivery has failed|returned mail|"
    r"read:|not read:|accepted:|declined:|tentative:|"
    r"recall:|message recall)",
    re.IGNORECASE,
)
_AUTOMATIC_SENDER = re.compile(r"^(mailer-daemon|postmaster|no-?reply|microsoftexchange)", re.I)
_OUT_OF_OFFICE = re.compile(
    r"\b(i am|i'm) (currently )?(out of (the )?office|on (annual )?leave|away)\b", re.I
)


def without_a_model(
    *, subject: str | None, body: str | None, sender: str | None
) -> Classification | None:
    """The mail that needs no model to know it is not work, or ``None``.

    Only ever answers ``general``: a rule may say what is plainly *not* work,
    never what is, because a tender wrongly settled here would raise nothing
    and nobody would know. Anything that might be work goes to a model.
    """
    subject_text = (subject or "").strip()
    local = (sender or "").split("@", 1)[0].strip().lower()
    why = None
    if _AUTOMATIC_SUBJECT.match(subject_text):
        why = "its subject is one a mail server or calendar writes"
    elif _AUTOMATIC_SENDER.match(local):
        why = "it was sent by a mail server, not a person"
    elif _OUT_OF_OFFICE.search((body or "")[:600]) and len(body or "") < 1500:
        why = "it is an out-of-office reply"
    if why is None:
        return None
    return Classification(
        category=MailCategory.GENERAL,
        confidence=0.95,
        title=subject_text[:200],
        summary="An automatic message; nothing to act on.",
        reasoning=f"Settled without a model: {why}.",
    )


def _shape() -> str:
    """The answer's keys, each with what goes in it — the schema in words."""
    lines = []
    for key in SCHEMA["required"]:
        spec = SCHEMA["properties"][key]
        if "enum" in spec:
            what = "one of " + " | ".join(spec["enum"]) + ". " + spec.get("description", "")
        elif spec.get("type") == "array":
            what = "list of strings. " + spec.get("description", "")
        else:
            what = f"{spec.get('type')}. " + spec.get("description", "")
        lines.append(f'  "{key}": {what}')
    return "{\n" + "\n".join(lines) + "\n}"


def _usable(payload: dict[str, Any]) -> bool:
    """An answer worth acting on: a real category and a confidence."""
    return str(payload.get("category") or "") in {c.value for c in MailCategory} and (
        payload.get("confidence") is not None
    )


class Classifier:
    """Reads one message. Holds no state beyond the model."""

    def __init__(self, settings: Settings, model: TextModel | None = None) -> None:
        self._settings = settings
        self._model = model

    @property
    def available(self) -> bool:
        return self._model is not None and self._model.configured

    async def classify(
        self, *, subject: str | None, body: str | None, sender: str | None
    ) -> Classification:
        """What this message is, and what it says.

        Automatic mail is settled here with no model at all. Otherwise the free
        model is asked, and Claude after it; when none of them answers, this
        raises, and the pipeline leaves the message alone and records why —
        the same outcome as a message it could not understand, and the safe one.
        """
        settled = without_a_model(subject=subject, body=body, sender=sender)
        if settled is not None:
            return settled
        if not self.available:
            raise ClassifierError(
                "No model is configured (LLM_PROVIDERS), so mail cannot be read."
            )

        content = (
            f"From: {sender or 'unknown'}\n"
            f"Subject: {subject or '(no subject)'}\n\n"
            # The free tiers take a few thousand tokens a minute. The newest
            # part of a thread is at the top, and that is what is judged.
            f"{trim((body or '').strip(), self._settings.llm_max_input_chars) or '(no body)'}"
        )
        system = (
            f"{_INSTRUCTIONS}\n"
            "Reply with one JSON object and nothing else — no prose, no code fence — "
            f"with exactly these keys:\n{_shape()}"
        )
        answer = await self._model.ask(
            system=system, user=content, max_tokens=1500, check=_usable
        )
        if answer is None:
            raise ClassifierError(
                "No model answered: every provider in LLM_PROVIDERS refused or failed."
            )
        payload = answer.payload
        return Classification(
            category=str(payload.get("category") or MailCategory.UNKNOWN),
            confidence=_as_float(payload.get("confidence")),
            is_reopened=bool(payload.get("is_reopened")),
            title=str(payload.get("title") or "").strip(),
            customer=str(payload.get("customer") or "").strip(),
            references=[
                str(r).strip() for r in (payload.get("references") or []) if str(r).strip()
            ],
            deadline=str(payload.get("deadline") or "").strip(),
            summary=str(payload.get("summary") or "").strip(),
            reasoning=(
                f"{str(payload.get('reasoning') or '').strip()} ({answer.who})".strip()
            ),
            cost_usd=_cost_of(answer.input_tokens, answer.output_tokens) if answer.paid else 0.0,
        )


def _as_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


#: Rough, and rough on purpose. What matters is that the intake bill is
#: attributable to the feature rather than appearing as a lump on somebody's
#: account; the exact figure is on the invoice.
_INPUT_PER_MTOK = 1.0
_OUTPUT_PER_MTOK = 5.0


def _cost_of(read: int, written: int) -> float:
    return round(
        (read * _INPUT_PER_MTOK + written * _OUTPUT_PER_MTOK) / 1_000_000, 6
    )
