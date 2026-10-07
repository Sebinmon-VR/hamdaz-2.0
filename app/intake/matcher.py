"""Finding the Proposals row an email is about, when the names disagree.

The problem is that the mail says "KOC Pumping Stn. Bid — rev 2" and the list
says "Kuwait Oil Company — pumping station upgrade", and the reference numbers
are written differently or missing. Matching those needs a model. Showing a
model the whole list does not work: at a thousand rows it is tens of thousands
of tokens per email, and accuracy *falls* as the list grows, because a model
reading a long list reads it less carefully.

So the list is narrowed first, and only the survivors are read:

1. ``mirror.search`` — full-text and exact references, then vector similarity
   over those. Cheap, local, and bounded by the candidate set rather than the
   list. Comes back with about ten rows.
2. **this module** — the model reads those ten and says which one, or none.

The second stage is allowed to say none, and says it often. A wrong match on a
tender means telling the wrong person their bid reopened; a missed match means
raising a duplicate. Neither is free, and the threshold between them is a
setting rather than a constant here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.llm import TextModel
from app.intake.classifier import Classification
from app.models.proposal_index import ProposalIndexItem
from app.proposals.mirror import Candidate, Embedder, references_in, search

logger = logging.getLogger("hamdaz.intake.match")


@dataclass(slots=True)
class Match:
    """What the matcher concluded, and enough to see why."""

    item: ProposalIndexItem | None = None
    confidence: float = 0.0
    reason: str = ""
    #: The shortlist it chose from, already scored. Recorded on the intake row
    #: so a wrong answer can be seen to have been a close call rather than a
    #: wild guess — which is the difference between tuning it and distrusting it.
    considered: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.considered is None:
            self.considered = []

    @property
    def found(self) -> bool:
        return self.item is not None


def _describe(candidate: Candidate) -> dict[str, Any]:
    """One candidate as the model sees it, and as the audit row keeps it."""
    item = candidate.item
    return {
        "id": item.item_id,
        "title": item.title,
        "customer": item.end_user,
        "quote_no": item.quote_no,
        "status": item.effective_status or item.status,
        "deadline": item.deadline.isoformat() if item.deadline else None,
        "assigned_to": item.assigned_name,
        "open": item.is_open,
        "score": round(candidate.score, 3),
        "matched_reference": candidate.matched_reference,
    }


_INSTRUCTIONS = """\
You are given one incoming email and a shortlist of rows from a company's \
proposals list. Decide whether the email is about one of those rows.

The names will not match. The list is maintained by hand over years, so the \
same bid appears as "KOC Pumping Stn." in one place and "Kuwait Oil Company — \
pumping station upgrade" in another. Judge by what the work *is*: the customer, \
the equipment or scope, and any reference number.

- A shared tender or quote number is close to conclusive. Nobody types one by \
accident. But check the customer agrees too, because reference formats repeat \
across customers.
- The same customer alone is not a match. A large customer has many bids.
- If two rows both look right, return no match and say so. A duplicate raised \
is a nuisance; the wrong person told their bid reopened is worse.
- Return no match freely. Nothing in the shortlist being right is a normal and \
useful answer, and it is what happens when genuinely new work arrives.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "item_id": {
            "type": "string",
            "description": "The id of the matching row, or empty for no match.",
        },
        "confidence": {
            "type": "number",
            "description": "0 to 1, how sure you are it is that row.",
        },
        "reason": {
            "type": "string",
            "description": "One sentence: what made it that row, or why none fit.",
        },
    },
    "required": ["item_id", "confidence", "reason"],
}


class Matcher:
    """Narrows the list, then asks a model to pick from what is left."""

    def __init__(
        self, settings: Settings, embedder: Embedder, model: TextModel | None = None
    ) -> None:
        self._settings = settings
        self._embedder = embedder
        self._model = model

    async def find(
        self,
        session: AsyncSession,
        classification: Classification,
        *,
        subject: str | None = None,
        threshold: float = 0.7,
    ) -> Match:
        """The row this email is about, or nothing.

        References come from the extraction *and* from the raw subject line.
        The subject is included because a tender number is very often only in
        there — "RE: T-2291 clarification" — and the extraction, reading for
        meaning, sometimes leaves it out of the body summary.
        """
        references = list(
            dict.fromkeys(
                [*classification.references, *references_in(subject, classification.title)]
            )
        )
        query = classification.match_text or (subject or "")
        if not query and not references:
            return Match(reason="Nothing in the mail to search on.")

        shortlist = await search(
            session,
            self._embedder,
            query_text=query,
            references=references,
        )
        considered = [_describe(c) for c in shortlist]
        if not shortlist:
            return Match(reason="Nothing in the list resembled it.", considered=[])

        # A reference hit that is alone in the shortlist is not sent to a model
        # at all. Nobody writes somebody else's tender number, and paying for a
        # round trip to be told so is waste.
        exact = [c for c in shortlist if c.matched_reference]
        if len(exact) == 1 and exact[0].score >= 1.0:
            return Match(
                item=exact[0].item,
                confidence=0.95,
                reason=f"Reference {exact[0].matched_reference} matches exactly.",
                considered=considered,
            )

        if self._model is None or not self._model.configured:
            # Fall back to the arithmetic. Weaker, and honest about it: the
            # score already blends the reference, the wording and the meaning.
            best = shortlist[0]
            confident = min(best.score, 1.0)
            return Match(
                item=best.item if confident >= threshold else None,
                confidence=confident,
                reason="Scored locally; no model is configured to check it.",
                considered=considered,
            )

        decision = await self._adjudicate(classification, subject, considered)
        chosen = next(
            (c.item for c in shortlist if c.item.item_id == decision.get("item_id")), None
        )
        confidence = _as_float(decision.get("confidence"))
        reason = str(decision.get("reason") or "").strip()

        if chosen is None or confidence < threshold:
            return Match(
                item=None,
                confidence=confidence,
                reason=reason or "No row in the shortlist was a confident match.",
                considered=considered,
            )
        return Match(item=chosen, confidence=confidence, reason=reason, considered=considered)

    async def _adjudicate(
        self,
        classification: Classification,
        subject: str | None,
        considered: list[dict[str, Any]],
    ) -> dict[str, Any]:
        mail = json.dumps(
            {
                "subject": subject,
                "title": classification.title,
                "customer": classification.customer,
                "references": classification.references,
                "summary": classification.summary,
            },
            ensure_ascii=False,
        )
        rows = json.dumps(considered, ensure_ascii=False)
        ids = {str(row["id"]) for row in considered}

        def usable(payload: dict[str, Any]) -> bool:
            # A row that is not on the shortlist is a made-up answer, not a match.
            return str(payload.get("item_id") or "") in ids | {""}

        system = (
            f"{_INSTRUCTIONS}\n"
            "Reply with one JSON object and nothing else — no prose, no code fence — "
            'with exactly these keys: {"item_id": the id of the matching row, or "" '
            'for no match; "confidence": 0 to 1; "reason": one sentence}.'
        )
        answer = await self._model.ask(
            system=system,
            user=f"EMAIL:\n{mail}\n\nSHORTLIST:\n{rows}",
            max_tokens=600,
            # Claude, when it comes to Claude, on the main model rather than
            # the extraction one: which row an email is about is judgement,
            # and a wrong answer files it on the wrong bid.
            claude_model=self._settings.anthropic_model,
            check=usable,
        )
        if answer is None:
            logger.warning("match adjudication: no model answered")
            return {"item_id": "", "confidence": 0.0, "reason": "No model answered."}
        decision = dict(answer.payload)
        decision["reason"] = f"{str(decision.get('reason') or '').strip()} ({answer.who})".strip()
        return decision


def _as_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
