"""Mail watch, step 2: what a mail from the CEO is, and what it says.

Four categories, as the presales team asked for them:

* **new_tender** — an invitation to bid: an Ariba event invitation, a portal's
  request for quote, a tender's documents being released.
* **negotiation** — a customer coming back on price for something quoted: a
  negotiation event, a request for the best and final offer.
* **general** — everything that needs no new work: reminders, a closed or
  reopened event, clarifications, bulletins, orders and invoices on the
  business network, portal access notices, internal mail.
* **confidential** — personal mail, and anything carrying a password or a
  one-time code. Only its date and the label are kept.

**Rules first.** Almost all of this mail is forwarded from Ariba and the
customer portals, and their subjects are fixed formats — read on 900 of his
mails, Aug–Oct 2026. Those are settled here without a model. What the rules
cannot place goes to Haiku when the settings allow; without it, the mail is
left as general with low confidence, which is what "unsure" means.

The details pulled out — reference numbers, the Ariba document number, the
customer, the closing date — are what step 3 matches against the Proposals
list, so they are read from the subject first and the forwarded header and
body second.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Final

from app.agents.model import AgentModel, Spent
from app.enquiries.claude import LLMError

NEW_TENDER: Final = "new_tender"
NEGOTIATION: Final = "negotiation"
GENERAL: Final = "general"
CONFIDENTIAL: Final = "confidential"
CATEGORIES: Final = (NEW_TENDER, NEGOTIATION, GENERAL, CONFIDENTIAL)

#: Below this the rules' answer is checked with the model, when allowed.
SURE: Final = 0.8

#: Ariba and the UAE portals state times in Gulf Standard Time.
GULF: Final = timezone(timedelta(hours=4))

#: How much of the body the model reads. The top is what the mail is about.
MODEL_BODY: Final = 4000


@dataclass
class Reading:
    category: str
    #: Finer than the category: invitation, reminder, closed, reopened,
    #: extended, cancelled, changed, clarification, buyer_message, order,
    #: invoice, portal_access, internal, personal, credential, other.
    kind: str
    #: eoi, rfi, rfq, rfp, rft, pre_tender or negotiation, when it says.
    stage: str | None = None
    #: "rules" or "model" — or "none" when neither could place it.
    decided_by: str = "rules"
    #: Which rule, or the model's reason in a few words.
    reason: str = ""
    confidence: float = 0.0
    title: str | None = None
    customer: str | None = None
    #: Tender, RFQ, PR and PO numbers, as found.
    references: list[str] = field(default_factory=list)
    #: The Ariba document number (``Doc340993208``): one per event, on every
    #: notice about it — the steadiest key there is.
    ariba_doc: str | None = None
    closing_at: datetime | None = None
    #: Who the forwarded mail was originally from.
    original_sender: str | None = None
    model_cost_usd: Decimal = Decimal("0")
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["closing_at"] = self.closing_at.isoformat() if self.closing_at else None
        out["model_cost_usd"] = str(self.model_cost_usd)
        return out


# ── reading the text ────────────────────────────────────────────────

_PREFIX = re.compile(r"^\s*(?:(?:fw|fwd|re|tr|aw)\s*:\s*)+", re.I)
_FROM = re.compile(r"From:\s*(?P<name>[^<\n]*?)\s*<(?P<email>[^>\s]+@[^>\s]+)>", re.I)
_DOC = re.compile(r"\bDoc(\d{6,})\b")
_EVENT_NO = re.compile(r"(?<!\d)([36]\d{9})(?!\d)")
_PR = re.compile(r"\bPR\s*[-:#]?\s*(\d{7,10})\b", re.I)
_PO = re.compile(r"\b(?:purchase order|order(?: no\.?| #)?)\s*[-:#]?\s*(45\d{8})\b", re.I)
_TROJAN_NO = re.compile(r"Tender No\.?\s*:?\s*-?\s*(\d{4,})", re.I)
#: A portal RFQ's first line of material, as a title for it.
_MATERIAL = re.compile(r"UnitName\s+Company\s+Project\s+(.{5,90}?)(?:\s+\d|\s{2,}|$)")
_CUSTOMER = re.compile(r"your customer,\s*([^,]{3,80}),", re.I)
_INVITED = re.compile(
    r"^(?P<org>.+?)\s+has invited you to participate in an event:\s*(?P<title>.+?)\.?\s*$", re.I
)
_EVENT_STATUS = re.compile(
    r"^event\s+(?P<title>.+?)\s*(?:-\s*)?(?P<status>end date reminder|is no longer accepting responses|"
    r"has been reopened|has been extended|has changed|has been changed|is cancelled|has been cancelled|"
    r"opened envelope.*|bidding opens.*|is now open)\.?\s*$",
    re.I,
)
_STATUS_KIND: Final = {
    "end date reminder": "reminder",
    "is no longer accepting responses": "closed",
    "has been reopened": "reopened",
    "has been extended": "extended",
    "has changed": "changed",
    "has been changed": "changed",
    "is cancelled": "cancelled",
    "has been cancelled": "cancelled",
    "is now open": "opened",
}
_ENDS_ON = re.compile(
    r"scheduled to end on \w+,\s*(?P<date>\w+ \d{1,2}, \d{4}) at (?P<time>\d{1,2}:\d{2} [AP]M)", re.I
)
_EXPIRY = re.compile(
    r"Tender Expiry Date\s*:\s*(?P<date>\d{1,2} \w{3} \d{4})\s*(?P<time>\d{1,2}:\d{2} ?[AP]M)?", re.I
)

_CONFIDENTIAL = re.compile(
    r"\b(otp|one[- ]time (?:password|code)|verification code|sign[- ]in (?:code|verification)|"
    r"password reset|reset your password|security code|payslip|salary|medical report|"
    r"bank statement|personal)\b",
    re.I,
)
_PORTAL_ACCESS = re.compile(
    r"(locked out of the event|access to the event .* has been restored|trade license|"
    r"icv certificate|ariba account|registration (?:approved|rejected)|supplier registration)",
    re.I,
)
_NETWORK = re.compile(
    r"(sap business network|\binvoice\b|service sheet|ready to invoice|notice of new receipt|"
    r"received a new order|purchase order|business lead)",
    re.I,
)
_NEGOTIATION = re.compile(
    r"(\bnegotiation\b|\bnego\b|\bneg\b|\bnego\s*v?\d|\bbafo\b|best and final|\bdiscount|"
    r"revised commercial|updated price|revised price|price revision|"
    r"revise your (?:price|pricing|offer)|"
    r"discounted commercial offer|commercial negotiation)",
    re.I,
)
_NEGOTIATION_BODY = re.compile(
    r"(best and final|discounted commercial offer|revise your (?:submitted )?(?:price|pricing|offer)|"
    r"review your submitted pricing|provide your best|final discount)",
    re.I,
)
_RELEASE = re.compile(
    r"(advance rft|tender release|upcoming tender|invitation to (?:bid|tender)|request for quot\w*|"
    r"request for proposal|\bitb\b|\bitt\b)",
    re.I,
)
_UPDATE = re.compile(
    r"(clarification|bulletin|addendum|corrigendum|extension|extended|revision of bid closing|"
    r"pre-?bid|meeting|submission|completion of|awarded|award|result|cancel+ed|reminder|"
    r"technical bid|query|queries)",
    re.I,
)
_TENDER_WORD = re.compile(
    r"\b(eoi\d?|rfi|rfq|rfp|rft|tender|expression of interest|request for quot\w*)\b", re.I
)
_STAGE_TOKENS: Final = (
    (re.compile(r"pre[- ]?tender", re.I), "pre_tender"),
    (re.compile(r"\beoi\d?\b|expression of interest", re.I), "eoi"),
    (re.compile(r"\brfi\b|request for information", re.I), "rfi"),
    (re.compile(r"\brfq\.?\b|\brfq(?=\d)|request for quot\w*", re.I), "rfq"),
    (re.compile(r"\brfp\b|\brfp(?=[-\d])|request for proposal", re.I), "rfp"),
    (re.compile(r"\brft\b|\brft(?=[-\d])|\btender\b|\bitt\b", re.I), "rft"),
)


def clean_subject(subject: str | None) -> str:
    return _PREFIX.sub("", subject or "").strip()


def stage_of(text: str) -> str | None:
    """The tender stage a title names; negotiation wins, otherwise the first
    stage word in the text — "Advance RFT documents … EOI for X" is the RFT."""
    if _NEGOTIATION.search(text):
        return "negotiation"
    found: list[tuple[int, str]] = []
    for pattern, stage in _STAGE_TOKENS:
        if match := pattern.search(text):
            found.append((match.start(), stage))
    return min(found)[1] if found else None


def _gulf(date: str, clock: str | None, formats: tuple[str, ...]) -> datetime | None:
    raw = f"{date} {clock}".strip() if clock else date
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw.replace("  ", " "), fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=GULF).astimezone(UTC)
    return None


def closing_date(body: str) -> datetime | None:
    if m := _ENDS_ON.search(body):
        return _gulf(m["date"], m["time"], ("%B %d, %Y %I:%M %p",))
    if m := _EXPIRY.search(body):
        clock = (m["time"] or "").replace("AM", " AM").replace("PM", " PM").replace("  ", " ")
        return _gulf(m["date"], clock or None, ("%d %b %Y %I:%M %p", "%d %b %Y"))
    return None


def references(*texts: str) -> list[str]:
    """Tender, PR, PO and portal numbers, in the order met, each once."""
    out: list[str] = []
    for text in texts:
        out += _EVENT_NO.findall(text)
        out += [f"PR {n}" for n in _PR.findall(text)]
        out += [f"PO {n}" for n in _PO.findall(text)]
        out += [f"Tender {n}" for n in _TROJAN_NO.findall(text)]
    return list(dict.fromkeys(out))


def _details(reading: Reading, subject: str, body: str) -> Reading:
    """Fill in what the text says, wherever the rule did not already."""
    head = body[:1500]
    if reading.original_sender is None and (m := _FROM.search(head)):
        reading.original_sender = m["email"].lower()
    if reading.ariba_doc is None and (m := _DOC.search(subject) or _DOC.search(head)):
        reading.ariba_doc = f"Doc{m.group(1)}"
    if not reading.references:
        reading.references = references(subject, head)
    if reading.customer is None and (m := _CUSTOMER.search(body)):
        reading.customer = m.group(1).strip()
    if reading.closing_at is None:
        reading.closing_at = closing_date(body)
    if reading.stage is None and reading.category in (NEW_TENDER, NEGOTIATION):
        reading.stage = stage_of(reading.title or subject)
    if reading.title is None and reading.category != CONFIDENTIAL:
        title = _DOC.sub("", subject).strip(" -:")
        reading.title = title or None
    return reading


# ── the rules ───────────────────────────────────────────────────────


def by_rules(subject: str | None, body: str | None) -> Reading:
    """The rules' answer. ``confidence`` below ``SURE`` means "ask the model"."""
    s = clean_subject(subject)
    b = body or ""
    head = b[:1500]

    if _CONFIDENTIAL.search(s):
        kind = "personal" if re.search(r"payslip|salary|medical|bank|personal", s, re.I) else "credential"
        return Reading(CONFIDENTIAL, kind, reason="confidential words in the subject", confidence=0.95)

    if _PORTAL_ACCESS.search(s):
        return _details(
            Reading(GENERAL, "portal_access", reason="portal access notice", confidence=0.95), s, b
        )

    if m := _INVITED.match(s):
        title = m["title"].strip()
        negotiating = bool(_NEGOTIATION.search(title))
        reading = Reading(
            NEGOTIATION if negotiating else NEW_TENDER,
            "invitation",
            reason="Ariba event invitation" + (" to a negotiation" if negotiating else ""),
            confidence=0.97,
            title=title,
            customer=m["org"].strip(),
        )
        return _details(reading, s, b)

    if m := _EVENT_STATUS.match(s):
        status = m["status"].lower()
        kind = next((v for k, v in _STATUS_KIND.items() if status.startswith(k)), "changed")
        if status.startswith("opened envelope"):
            kind = "envelope_opened"
        reading = Reading(
            GENERAL, kind, reason=f"Ariba event notice: {status}", confidence=0.97,
            title=m["title"].strip(" -"),
        )
        reading.stage = stage_of(reading.title or "")
        return _details(reading, s, b)

    if _NETWORK.search(s):
        kind = "invoice" if re.search(r"invoice|service sheet|receipt", s, re.I) else "order"
        return _details(
            Reading(GENERAL, kind, reason="business network order or invoice notice", confidence=0.95),
            s, b,
        )

    if re.search(r"procurement portal\s*:\s*request for quot", s, re.I):
        reading = Reading(
            NEW_TENDER, "invitation", stage="rfq", reason="portal request for quote",
            confidence=0.95,
        )
        if re.search(r"trojan", s, re.I):
            reading.customer = "Trojan Construction Group"
        if m := _TROJAN_NO.search(s):
            what = _MATERIAL.search(b)
            reading.title = f"Tender {m.group(1)}" + (f" - {what.group(1).strip()}" if what else "")
        return _details(reading, s, b)

    if re.search(r"quote approval needed", s, re.I):
        return _details(Reading(GENERAL, "internal", reason="this app's quote approval mail", confidence=0.95), s, b)

    if _NEGOTIATION.search(s) or _NEGOTIATION_BODY.search(head):
        where = "subject" if _NEGOTIATION.search(s) else "body"
        return _details(
            Reading(NEGOTIATION, "buyer_message", stage="negotiation",
                    reason=f"negotiation words in the {where}", confidence=0.9),
            s, b,
        )

    if _RELEASE.search(s):
        return _details(
            Reading(NEW_TENDER, "release", reason="tender documents or request released",
                    confidence=0.85),
            s, b,
        )

    is_ariba = bool(_DOC.search(s) or "ariba.com" in head.lower())
    if _UPDATE.search(s) and (is_ariba or _TENDER_WORD.search(s)):
        kind = "extended" if re.search(r"extension|extended|revision of bid closing", s, re.I) else (
            "clarification" if re.search(r"clarification|query|queries", s, re.I) else "buyer_message"
        )
        return _details(
            Reading(GENERAL, kind, reason="update on a tender already running", confidence=0.85), s, b
        )

    if is_ariba:
        # A buyer's message on an event: about a tender already invited.
        return _details(
            Reading(GENERAL, "buyer_message", reason="Ariba message on an existing event",
                    confidence=0.75),
            s, b,
        )

    if _TENDER_WORD.search(s):
        return _details(
            Reading(NEW_TENDER, "other", reason="tender word in the subject", confidence=0.6), s, b
        )

    return _details(
        Reading(GENERAL, "other", decided_by="none", reason="no rule matched", confidence=0.3), s, b
    )


# ── the model, for what the rules leave ─────────────────────────────

SYSTEM: Final = """You sort email for the presales team of Hamdaztech, an IT
company in Abu Dhabi. Every mail was sent or forwarded by the CEO. Most are
forwarded from SAP Ariba or customer procurement portals (ADNOC and others).

Categories:
- new_tender: an invitation to bid or quote, or tender documents being released
  (EOI, RFI, RFQ, RFP, RFT/tender). New work for presales.
- negotiation: the customer comes back on price or terms for something already
  quoted: a negotiation round, best and final offer, discount request.
- general: needs no new work — reminders, clarifications, bulletins, deadline
  extensions, results, orders, invoices, internal or company announcements.
- confidential: personal mail (family, health, salary, private matters) or
  anything carrying a password, one-time code or account credentials.

Be honest with confidence: below 0.6 means you are guessing. Copy reference
numbers exactly as written. Use empty strings and an empty list when the mail
does not say."""

SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "category", "kind", "stage", "confidence", "reason", "title", "customer",
        "references", "closing_date",
    ],
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "kind": {
            "type": "string",
            "enum": [
                "invitation", "release", "buyer_message", "clarification", "extended",
                "reminder", "order", "invoice", "internal", "announcement", "personal",
                "credential", "other",
            ],
        },
        "stage": {
            "type": "string",
            "enum": ["", "eoi", "rfi", "rfq", "rfp", "rft", "pre_tender", "negotiation"],
        },
        "confidence": {"type": "number"},
        "reason": {"type": "string", "description": "Why, in under 15 words."},
        "title": {"type": "string", "description": "What the work is, as a short task title."},
        "customer": {"type": "string"},
        "references": {"type": "array", "items": {"type": "string"}},
        "closing_date": {
            "type": "string",
            "description": "The bid closing date and time as ISO 8601 with offset, or empty.",
        },
    },
}


async def by_model(
    model: AgentModel,
    *,
    subject: str | None,
    body: str | None,
    attachments: list[str] | None,
    rules: Reading,
) -> Reading:
    """Haiku's answer, keeping what the rules found that it did not."""
    spent = Spent()
    content = [
        {
            "type": "text",
            "text": (
                f"Subject: {subject or ''}\n"
                f"Attachments: {', '.join(attachments or []) or 'none'}\n"
                f"Rules' guess: {rules.category} ({rules.reason})\n\n"
                f"{(body or '')[:MODEL_BODY]}"
            ),
        }
    ]
    answer = await model.ask(
        system=SYSTEM, content=content, schema=SCHEMA, user_key="mailwatch",
        spent=spent, max_tokens=1500,
    )
    closing = None
    if raw := (answer.get("closing_date") or "").strip():
        try:
            closing = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if closing.tzinfo is None:
                closing = closing.replace(tzinfo=GULF)
            closing = closing.astimezone(UTC)
        except ValueError:
            closing = None
    category = answer.get("category") if answer.get("category") in CATEGORIES else GENERAL
    confidential = category == CONFIDENTIAL
    return Reading(
        category=category,
        kind=answer.get("kind") or "other",
        stage=(answer.get("stage") or None) if not confidential else None,
        decided_by="model",
        reason=(answer.get("reason") or "")[:300],
        confidence=max(0.0, min(1.0, float(answer.get("confidence") or 0))),
        title=None if confidential else (answer.get("title") or rules.title),
        customer=None if confidential else (answer.get("customer") or rules.customer),
        references=[] if confidential else list(
            dict.fromkeys([*rules.references, *(answer.get("references") or [])])
        ),
        ariba_doc=None if confidential else rules.ariba_doc,
        closing_at=None if confidential else (closing or rules.closing_at),
        original_sender=rules.original_sender,
        model_cost_usd=spent.cost(model.model),
    )


async def classify(
    *,
    subject: str | None,
    body: str | None,
    attachments: list[str] | None,
    model: AgentModel | None,
    use_model: bool,
) -> Reading:
    """Rules, then Haiku when the rules are unsure and the settings allow it.

    A model failure leaves the rules' answer standing, with the error noted —
    a mail is never lost because a call failed.
    """
    rules = by_rules(subject, body)
    if rules.confidence >= SURE or not use_model or model is None or not model.configured:
        return _scrubbed(rules)
    try:
        return _scrubbed(
            await by_model(model, subject=subject, body=body, attachments=attachments, rules=rules)
        )
    except (LLMError, ValueError, TypeError) as exc:
        rules.error = f"model: {exc}"[:500]
        return _scrubbed(rules)


def _scrubbed(reading: Reading) -> Reading:
    """A confidential mail keeps its label and nothing it says."""
    if reading.category == CONFIDENTIAL:
        reading.title = reading.customer = reading.ariba_doc = reading.stage = None
        reading.references = []
        reading.closing_at = None
    return reading
