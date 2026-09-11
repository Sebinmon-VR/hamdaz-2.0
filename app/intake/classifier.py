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

Anthropic rather than OpenAI, following the quote extraction already in this
codebase: the same job — read a document, return a fixed shape — and no reason
for two providers to own one kind of work. The embeddings in the matcher are
OpenAI's because Anthropic does not offer any.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
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


class Classifier:
    """Reads one message. Holds no state beyond the client."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self._settings.anthropic_api_key)

    def _anthropic(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key
            )
        return self._client

    async def classify(
        self, *, subject: str | None, body: str | None, sender: str | None
    ) -> Classification:
        """What this message is, and what it says.

        A model that is not configured, or that fails, returns ``unknown`` with
        no confidence rather than raising. The pipeline then leaves the message
        alone and records why — which is the same outcome as a message it could
        not understand, and is the safe one.
        """
        if not self.available:
            raise ClassifierError(
                "No Anthropic API key is configured, so mail cannot be read."
            )

        content = (
            f"From: {sender or 'unknown'}\n"
            f"Subject: {subject or '(no subject)'}\n\n"
            f"{(body or '').strip() or '(no body)'}"
        )
        try:
            response = await self._anthropic().messages.create(
                model=self._settings.extract_model,
                max_tokens=1500,
                system=_INSTRUCTIONS,
                tools=[
                    {
                        "name": "record",
                        "description": "Record what this message is and what it says.",
                        "input_schema": SCHEMA,
                    }
                ],
                # Forced, so the answer is always the shape above and never a
                # paragraph of prose the caller has to parse.
                tool_choice={"type": "tool", "name": "record"},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the intake row
            raise ClassifierError(f"{type(exc).__name__}: {exc}") from exc

        payload = _tool_input(response)
        if payload is None:
            raise ClassifierError("The model did not answer in the expected shape.")

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
            reasoning=str(payload.get("reasoning") or "").strip(),
            cost_usd=_cost_of(response, self._settings),
        )


def _tool_input(response: Any) -> dict[str, Any] | None:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            value = getattr(block, "input", None)
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return None
    return None


def _as_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


#: Rough, and rough on purpose. What matters is that the intake bill is
#: attributable to the feature rather than appearing as a lump on somebody's
#: account; the exact figure is on the invoice.
_INPUT_PER_MTOK = 5.0
_OUTPUT_PER_MTOK = 25.0


def _cost_of(response: Any, settings: Settings) -> float:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0.0
    read = int(getattr(usage, "input_tokens", 0) or 0)
    written = int(getattr(usage, "output_tokens", 0) or 0)
    return round(
        (read * _INPUT_PER_MTOK + written * _OUTPUT_PER_MTOK) / 1_000_000, 6
    )
