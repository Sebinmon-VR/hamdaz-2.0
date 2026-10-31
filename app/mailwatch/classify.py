"""Mail watch, step 2: what a mail from the CEO is, and what it says.

Five categories, as the presales team asked for them:

* **new_tender** — an invitation to bid: an Ariba event invitation, a portal's
  request for quote, a tender's documents being released.
* **negotiation** — a customer coming back on price for something quoted: a
  negotiation event, a request for the best and final offer.
* **order** — a customer's purchase order: the order confirmation presales
  is waiting for. On SAP Business Network one order brings two mails a minute
  apart — the purchase order itself and a short "you have received a new
  order" — plus a weekly reminder of the orders not yet confirmed.
* **general** — everything that needs no new work: reminders, a closed or
  reopened event, clarifications, bulletins, invoices on the business
  network, portal access notices, internal mail.
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
ORDER: Final = "order"
GENERAL: Final = "general"
CONFIDENTIAL: Final = "confidential"
CATEGORIES: Final = (NEW_TENDER, NEGOTIATION, ORDER, GENERAL, CONFIDENTIAL)

#: An order's kinds. The first is the purchase order itself — the one with the
#: amount, terms and the buyer's references — and the one its card is made from.
PURCHASE_ORDER: Final = "purchase_order"
ORDER_NOTICE: Final = "order_notice"
ORDER_CHANGED: Final = "order_changed"
ORDER_CANCELLED: Final = "order_cancelled"
#: The weekly list of orders not yet confirmed: an order mail, never a card.
CONFIRM_REMINDER: Final = "confirm_reminder"
#: The kinds that are an order arriving or changing — each gets a card.
ORDER_KINDS: Final = (PURCHASE_ORDER, ORDER_NOTICE, ORDER_CHANGED, ORDER_CANCELLED)

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
    #: extended, cancelled, changed, clarification, buyer_message,
    #: purchase_order, order_notice, order_changed, order_cancelled,
    #: confirm_reminder, invoice, portal_access, internal, personal,
    #: credential, other.
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


#: "Abu Dhabi National Oil Company (ADNOC) sent a new Purchase Order 4500166918"
_PO_SENT = re.compile(
    r"^(?P<org>.+?)\s+sent\s+(?:you\s+)?an?\s+(?P<what>new|changed|updated|cancell?ed)?\s*"
    r"purchase order\s*(?P<no>\d{6,})?",
    re.I,
)
#: "You have received a new order 4500166918"
_ORDER_NOTICE = re.compile(
    r"^you have received an?\s+(?P<what>new|changed|updated|cancell?ed)?\s*order\s*(?P<no>\d{6,})?",
    re.I,
)
_CONFIRM_ORDERS = re.compile(r"^confirm orders from your buyers", re.I)
#: A customer's order outside the business network: "LPO for …", "PO No: 12345".
_ORDER_WORDS = re.compile(
    r"\b(lpo|local purchase order|purchase order|"
    r"p\.?o\.?\s*(?:no\.?|number|#)?\s*[:#-]?\s*\d{5,})\b",
    re.I,
)
_NOT_AN_ORDER = re.compile(r"(invoice|service sheet|receipt|payment|remittance)", re.I)
_ORDER_NO = re.compile(r"(?<!\d)(45\d{8})(?!\d)")
#: A tender number named as one, anywhere in an order: "RFQ 6000142474".
_ORDER_TENDER = re.compile(
    r"\b(?:rfq|rfp|rft|rfi|eoi|tender|event)\s*(?:no\.?)?\s*[-:#]?\s*([36]\d{9})(?!\d)", re.I
)


@dataclass
class Order:
    """What a purchase order mail says, as far as it says it. SAP Business
    Network's two mails each carry part: the purchase order has the terms and
    the buyer's references, the short notice the order date."""

    number: str | None = None
    #: New, Changed or Cancelled, as the order says.
    status: str | None = None
    customer: str | None = None
    #: The customer's entity that placed it ("Abu Dhabi Marine Operations…").
    buyer: str | None = None
    amount: str | None = None
    currency: str | None = None
    order_date: str | None = None
    version: str | None = None
    payment_terms: str | None = None
    #: The order's header comments, where buyers put the PR and RFQ.
    header: str | None = None
    project: str | None = None
    #: What the customer wrote above the order.
    message: str | None = None
    tenders: list[str] = field(default_factory=list)
    prs: list[str] = field(default_factory=list)

    def references(self) -> list[str]:
        """Tender numbers, then PRs, then the order's own number — the
        order matching tries them in that order."""
        return list(dict.fromkeys([
            *self.tenders, *(f"PR {n}" for n in self.prs),
            *([f"PO {self.number}"] if self.number else []),
        ]))


def _status(word: str | None) -> str:
    word = (word or "new").lower()
    if word.startswith("cancel"):
        return "Cancelled"
    return "Changed" if word in ("changed", "updated") else "New"


def read_order(subject: str | None, body: str | None) -> Order:
    """The order's details from a business network mail. Each is left empty
    when the mail does not say it — a customer's own LPO mail says little."""
    s = clean_subject(subject)
    text_value = body or ""
    lines = [line.strip() for line in text_value.splitlines() if line.strip()]
    order = Order()

    if m := _PO_SENT.match(s):
        order.customer, order.number = m["org"].strip(), m["no"]
        order.status = _status(m["what"])
    elif m := _ORDER_NOTICE.match(s):
        order.number, order.status = m["no"], _status(m["what"])

    def same_line(label: str) -> str | None:
        found = re.search(rf"^\s*{label}[ \t]+(\S.*?)\s*$", text_value, re.M | re.I)
        return found.group(1) if found else None

    # The short notice: "Customer  ADNOC" / "Order No.  45…" / "Total amount  1,237.95 AED".
    order.customer = order.customer or same_line("Customer")
    if not order.number and (m := _ORDER_NO.search(same_line(r"Order No\.?") or "")):
        order.number = m.group(1)
    order.order_date = same_line("Order Date")
    total = same_line("Total amount") or ""
    if m := re.match(r"([\d,]+(?:\.\d+)?)\s*([A-Z]{3})?", total):
        order.amount, order.currency = m.group(1), m.group(2)

    # The purchase order: "Purchase Order" / "(New)" / "45…" / "Amount:" / "1,237.95" / "AED".
    for i, line in enumerate(lines[:-1]):
        if line.lower() == "purchase order" and re.fullmatch(r"\(\w+\)", lines[i + 1]):
            order.status = _status(lines[i + 1].strip("()"))
            if i + 2 < len(lines) and (m := _ORDER_NO.fullmatch(lines[i + 2])):
                order.number = order.number or m.group(1)
            break
    for i, line in enumerate(lines[:-1]):
        if line.rstrip(":").lower() == "amount" and re.fullmatch(r"[\d,]+(?:\.\d+)?", lines[i + 1]):
            order.amount = lines[i + 1]
            if i + 2 < len(lines) and re.fullmatch(r"[A-Z]{3}", lines[i + 2]):
                order.currency = lines[i + 2]
            break
    if m := re.search(r"^\s*Version:\s*(\d+)", text_value, re.M):
        order.version = m.group(1)
    if "payment terms" in (low := [line.lower() for line in lines]):
        start = low.index("payment terms") + 1
        terms = []
        for line in lines[start:start + 3]:
            if line.lower() in ("comments", "contact information"):
                break
            terms.append(line)
        order.payment_terms = " · ".join(terms) or None
    if m := re.search(
        r"Header Text\s*:\s*(.+?)"
        r"(?:\n\s*(?:Contact Information|Supplier Address|Other Information)\b|\Z)",
        text_value, re.S | re.I,
    ):
        order.header = re.sub(r"\s+", " ", m.group(1)).strip()[:400] or None
    if m := re.search(r"Project\s*:\s*([^\n]{3,120})", text_value, re.I):
        order.project = re.split(r"\s+PA Ref#|\s{2,}", m.group(1).strip())[0].strip(" -") or None
    for i, line in enumerate(lines[:-2]):
        # "From:" / "Customer" / the buyer's entity, its address below.
        if line == "From:" and lines[i + 1].lower() == "customer":
            order.buyer = lines[i + 2]
            break
    if m := re.search(
        r"Message from your customer[^\n]*\n(.+?)(?:\n\s*\[https?:|\n\s*Process order|\Z)",
        text_value, re.S | re.I,
    ):
        order.message = re.sub(r"\s+", " ", m.group(1)).strip()[:700] or None

    where = " ".join(filter(None, [order.header, s]))
    order.tenders = list(dict.fromkeys(
        _EVENT_NO.findall(order.header or "") + _ORDER_TENDER.findall(text_value)
    ))
    order.prs = list(dict.fromkeys(
        _PR.findall(where) + re.findall(r"\bPR\s*(?:Ref|No)\.?\s*#?\s*:?\s*(\d{7,12})", where, re.I)
    ))
    return order


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


def _order_reading(s: str, b: str, kind: str, reason: str, confidence: float) -> Reading:
    """An order, with what matching needs: the tender and PR numbers the
    buyer quoted, the order number, and the project as a title when there is
    one — "PO 45…" otherwise, which the title search leaves alone."""
    order = read_order(s, b)
    reading = Reading(
        ORDER, kind, reason=reason, confidence=confidence,
        customer=order.customer,
        title=order.project or (f"PO {order.number}" if order.number else None),
        references=order.references(),
    )
    return _details(reading, s, b)


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

    if m := _PO_SENT.match(s):
        kind = {"Changed": ORDER_CHANGED, "Cancelled": ORDER_CANCELLED}.get(
            _status(m["what"]), PURCHASE_ORDER
        )
        return _order_reading(s, b, kind, "business network purchase order", 0.97)

    if _ORDER_NOTICE.match(s):
        return _order_reading(s, b, ORDER_NOTICE, "business network new-order notice", 0.97)

    if _CONFIRM_ORDERS.match(s):
        reading = Reading(
            ORDER, CONFIRM_REMINDER, reason="business network reminder of unconfirmed orders",
            confidence=0.97, title="Orders waiting for confirmation",
            references=[f"PO {n}" for n in dict.fromkeys(_ORDER_NO.findall(b))],
        )
        return _details(reading, s, b)

    if _ORDER_WORDS.search(s) and not _NOT_AN_ORDER.search(s) and not _NEGOTIATION.search(s):
        # Words alone: worth the model's look when it is allowed one.
        return _order_reading(s, b, PURCHASE_ORDER, "order words in the subject", 0.7)

    if _NETWORK.search(s):
        kind = "invoice" if re.search(r"invoice|service sheet|receipt", s, re.I) else "network"
        return _details(
            Reading(GENERAL, kind, reason="business network invoice or notice", confidence=0.95),
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
- order: a customer places, changes or cancels an order with Hamdaztech — a
  purchase order (PO, LPO), or a notice that one was sent. Not a supplier
  confirming an order Hamdaztech placed with them, and not an invoice.
- general: needs no new work — reminders, clarifications, bulletins, deadline
  extensions, results, invoices, internal or company announcements.
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
                "reminder", PURCHASE_ORDER, ORDER_CHANGED, ORDER_CANCELLED, "invoice",
                "internal", "announcement", "personal", "credential", "other",
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
    kind = answer.get("kind") or "other"
    found = list(rules.references)
    if category == ORDER:
        # The numbers an order is matched on, read as for the rules' orders.
        found += read_order(subject, body).references()
        if kind not in ORDER_KINDS:
            kind = PURCHASE_ORDER
    return Reading(
        category=category,
        kind=kind,
        stage=(answer.get("stage") or None) if not confidential else None,
        decided_by="model",
        reason=(answer.get("reason") or "")[:300],
        confidence=max(0.0, min(1.0, float(answer.get("confidence") or 0))),
        title=None if confidential else (answer.get("title") or rules.title),
        customer=None if confidential else (answer.get("customer") or rules.customer),
        references=[] if confidential else list(
            dict.fromkeys([*found, *(answer.get("references") or [])])
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
