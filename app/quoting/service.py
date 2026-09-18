"""The quote request workflow: who may do what, and when.

The loop, and the rules that keep it honest:

    draft ──submit──> pending approval ──rework──> changes requested ──submit──> …
                            │                              (back to the requester)
                            ├──approve──> approved  (the queue for Zoho)
                            └──reject───> rejected

**A request is editable only when it is with its requester** — in draft or after
a rework. Editing it while approvers are looking would mean they approved
something that no longer exists, which is the failure this whole module exists to
prevent.

**Approvers are the team's ``approver`` role holders**, plus managers and the
CEO. Not the requester: nobody approves their own quote, however senior.

**The requester chooses the supplier; the approver can overrule it.** The
requester attaches what they were sent, picks the offer to quote from, and the
quote is priced from that supplier's lines. An approver who disagrees picks a
different one when they decide, and the quote is repriced from it at the same
margin — approving supplier B while the document still carries supplier A's
prices would approve something nobody wrote. That moves the revision, so what
was approved is visibly not what was submitted.

Nothing here touches Zoho. Approval puts a request in a queue and stops.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comparison import SupplierQuote, SupplierQuoteItem
from app.models.quoting import (
    EDITABLE_STATUSES,
    CommentTarget,
    ComplianceArea,
    ComplianceStatus,
    CostStage,
    QuoteComment,
    QuoteComplianceItem,
    QuoteCostLine,
    QuoteRequest,
    QuoteRequestItem,
    QuoteReview,
    QuoteRevision,
    QuoteStatus,
    QuoteSubmissionField,
    ReviewAction,
    Severity,
)
from app.models.role import Role, UserRole
from app.roles.catalogue import SUPER_ADMIN
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.quoting import bidpack

logger = logging.getLogger("hamdaz.quoting")

#: Organisation-wide roles that may approve anywhere.
GLOBAL_APPROVERS = frozenset({"super_admin", "ceo", "manager"})
#: Roles held inside the team that may approve its quotes.
TEAM_APPROVERS = frozenset({"approver", "team_manager", "team_lead"})


class QuoteError(Exception):
    """A quote operation was refused. Safe to show a user."""


class QuoteNotFoundError(QuoteError):
    pass


class QuotePermissionError(QuoteError):
    pass


# ── who may do what ────────────────────────────────────────────────────


async def team_role_keys(
    session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID
) -> set[str]:
    keys = await session.scalars(
        select(Role.key)
        .join(TeamMembership, TeamMembership.role_id == Role.id)
        .where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
    )
    return set(keys.all())


async def may_approve(
    session: AsyncSession, request: QuoteRequest, *, user: User, roles: set[str]
) -> tuple[bool, str]:
    """Whether this person may decide this request, and why not if they may not."""
    if request.created_by_id == user.id and not (roles & {"super_admin"}):
        # Not a hierarchy question. A quote reviewed only by the person who wrote
        # it has not been reviewed.
        return False, "You cannot approve a quote you raised yourself."
    if roles & GLOBAL_APPROVERS:
        return True, "Approves anywhere."
    held = await team_role_keys(session, team_id=request.team_id, user_id=user.id)
    if held & TEAM_APPROVERS:
        return True, f"Holds {sorted(held & TEAM_APPROVERS)[0]!r} in this team."
    return False, (
        "Only an approver, team lead or manager of this team may decide a quote."
    )


async def approvers_for(session: AsyncSession, team_id: uuid.UUID) -> list[User]:
    """Everyone who could decide this team's quotes — who to notify, later."""
    rows = await session.scalars(
        select(TeamMembership)
        .join(Role, Role.id == TeamMembership.role_id)
        .where(TeamMembership.team_id == team_id, Role.key.in_(TEAM_APPROVERS))
    )
    people = {m.user_id: m.user for m in rows.all()}

    # Selecting the users, not the grant rows: ``UserRole`` has a ``role`` but no
    # ``user``, so reading one off it raised — and the caller that raised hardest
    # was the one mailing the approvers, where the failure is caught and turns
    # into nobody being told.
    globals_ = await session.scalars(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.key.in_(GLOBAL_APPROVERS))
    )
    for user in globals_.all():
        people.setdefault(user.id, user)
    return list(people.values())


def require_owner(request: QuoteRequest, *, user: User) -> None:
    """Whose quote it is.

    Separate from the status question deliberately: the two refusals are not the
    same refusal, and one field is allowed to move on a quote that is otherwise
    frozen. See ``require_editable`` and ``may_set_currency``.
    """
    if request.created_by_id != user.id and request.assigned_to_id != user.id:
        raise QuotePermissionError("This quote is not yours to edit.")


def may_set_currency(request: QuoteRequest, *, user: User, roles: set[str]) -> bool:
    """Who may state the currency.

    Whose quote it is, exactly as for every other write — plus a super admin,
    who holds every access in this module by definition and should not have to
    ask the author to correct a label.

    What is deliberately *not* in this rule is the status. Everything else on a
    quote freezes the moment it is submitted, and should: an approver has to
    decide on the document they were sent. The currency is outside that, because
    nothing in this module converts between currencies — the code is a label on
    figures that are already what they are, so setting it restates no number,
    moves no total and invalidates no approval. Freezing it too would mean
    pulling a quote back out of approval, and re-notifying every approver, to
    correct three letters.
    """
    if user.id in (request.created_by_id, request.assigned_to_id):
        return True
    return SUPER_ADMIN in roles


def require_editable(request: QuoteRequest, *, user: User) -> None:
    if request.status not in EDITABLE_STATUSES:
        raise QuotePermissionError(
            f"This quote is {request.status.replace('_', ' ')} and cannot be edited. "
            f"Editing it while approvers are looking would mean they approved "
            f"something that no longer exists."
        )
    require_owner(request, user=user)


# ── building one ───────────────────────────────────────────────────────


#: The Zoho-shaped half of the form: what an estimate is.
_ESTIMATE_FIELDS = (
    "title", "customer_name", "customer_id", "contact_person", "reference_number",
    "quote_date", "expiry_date", "currency", "salesperson_name", "place_of_supply",
    "payment_terms", "delivery_terms", "cf_bcd", "cf_portal", "subject", "notes",
    "terms", "reference",
)

#: The bid pack's own half: what a tender asks for and an estimate has no room
#: for. Every one of them nullable — a quote for somebody who rang up and asked
#: for a price fills in none of these and should not be nagged about it.
_BID_FIELDS = (
    "rfp_number", "buying_entity", "line_item_ref", "manufacturer_name",
    "manufacturer_part_number", "manufacturer_class_no", "incoterm_required",
    "incoterm_place", "ship_to", "requested_delivery_date", "delivery_days",
    "country_of_origin", "mode_of_shipment", "bid_validity_days", "bid_reference",
    "technical_verdict", "commercial_verdict", "supplier_currency",
)

#: Money and rates. Held apart because they go through ``Decimal`` rather than
#: being assigned as they arrive — a bid price that has been through a float is
#: a bid price with a rounding argument attached.
_DECIMAL_FIELDS = (
    "discount", "shipping_charge", "adjustment", "customs_duty_percent",
    "financing_rate_percent",
)

#: Decimals that mean "not set" when absent, rather than zero. A markup of
#: nothing and no markup at all are different answers: the first prices the bid
#: at cost, the second has not been decided yet.
_NULLABLE_DECIMALS = (
    "fx_rate", "target_markup_percent", "submission_unit_price", "submission_total",
)


def apply_fields(request: QuoteRequest, payload: dict[str, Any]) -> None:
    """Copy the form fields — the estimate's and the bid's — onto the row."""
    for field in _ESTIMATE_FIELDS + _BID_FIELDS:
        if field in payload:
            setattr(request, field, payload[field])
    for field in _DECIMAL_FIELDS:
        if payload.get(field) is not None:
            setattr(request, field, Decimal(str(payload[field])))
    for field in _NULLABLE_DECIMALS:
        if field in payload:
            value = payload[field]
            setattr(request, field, Decimal(str(value)) if value is not None else None)
    if payload.get("cash_exposure_days") is not None:
        request.cash_exposure_days = int(payload["cash_exposure_days"])
    for field in ("multiple_supplier_quotes", "discloses_principal_price"):
        if payload.get(field) is not None:
            setattr(request, field, bool(payload[field]))


def set_items(request: QuoteRequest, items: list[dict[str, Any]]) -> None:
    """Replace the line items wholesale.

    Replaced rather than merged: a quote's lines are a single document, and a
    partial update would need the caller to track ids that only exist after a
    save, for no benefit anybody asked for.
    """
    request.items = [
        QuoteRequestItem(
            position=position,
            name=item["name"],
            description=item.get("description"),
            item_code=item.get("item_code"),
            brand=item.get("brand"),
            unit=item.get("unit"),
            quantity=Decimal(str(item.get("quantity", 1))),
            rate=Decimal(str(item.get("rate", 0))),
            discount=Decimal(str(item.get("discount", 0))),
            tax_name=item.get("tax_name"),
            tax_percentage=(
                Decimal(str(item["tax_percentage"]))
                if item.get("tax_percentage") is not None
                else None
            ),
            cost_rate=(
                Decimal(str(item["cost_rate"])) if item.get("cost_rate") is not None else None
            ),
            source_supplier_quote_id=item.get("source_supplier_quote_id"),
        )
        for position, item in enumerate(items)
    ]


def set_cost_lines(request: QuoteRequest, rows: list[dict[str, Any]]) -> None:
    """Replace the landed-cost build-up, for the same reason the items are.

    Note what is *not* here. The goods are not a row — they come from the
    priced lines, so that repricing from another supplier carries the cost with
    it instead of leaving the old one at the top of the build-up. Duty and
    financing are not rows either; both are arithmetic on figures the request
    already holds. See ``app.quoting.bidpack``.

    **A row quoted in the supplier's currency converts itself.** Where a caller
    gives an amount in a foreign currency and the bid carries a rate for it, the
    figure in the quote's own currency is worked out here rather than accepted.
    Taking both would mean storing one number twice, and the pair would disagree
    the first time the rate was revised — which on a bid that stands for ninety
    days it always is. The quoted figure stays exactly as quoted; it is the
    conversion that is derived.
    """
    request.cost_lines = [
        QuoteCostLine(
            position=position,
            stage=row.get("stage") or CostStage.ORIGIN,
            label=row["label"],
            basis=row.get("basis"),
            amount_source=_decimal_or_none(row.get("amount_source")),
            source_currency=row.get("source_currency"),
            amount_base=_base_amount(request, row),
            is_principal=bool(row.get("is_principal")),
            is_firm=bool(row.get("is_firm")),
            notes=row.get("notes"),
        )
        for position, row in enumerate(rows)
    ]


def _base_amount(request: QuoteRequest, row: dict[str, Any]) -> Decimal:
    """One cost row in the quote's own currency.

    Converted from the quoted figure where the currencies and a rate line up,
    and otherwise taken as given — which covers the ordinary case of a cost
    that was only ever reckoned in our own money.
    """
    source = _decimal_or_none(row.get("amount_source"))
    currency = (row.get("source_currency") or "").upper()
    rate = request.fx_rate
    if (
        source is not None
        and rate
        and rate > 0
        and currency
        and currency != (request.currency or "").upper()
    ):
        return (source * rate).quantize(_RATE)
    return Decimal(str(row.get("amount_base") or 0))


def set_compliance(request: QuoteRequest, rows: list[dict[str, Any]]) -> None:
    """Replace the compliance matrix, keeping when each gap was actually closed.

    Replaced wholesale like everything else on the form, but a resolved row
    carries a date somebody will one day want — "when did we clear the ICV
    question" is a real question after a bid is lost. The caller sends the row's
    id back, so the original moment survives a save that touched something else
    entirely. A row with no id is new, and a row newly ticked is stamped now.
    """
    before = {row.id: row.resolved_at for row in request.compliance}
    now = datetime.now(UTC)
    request.compliance = [
        QuoteComplianceItem(
            position=position,
            ref=row.get("ref"),
            area=row.get("area") or ComplianceArea.COMMERCIAL,
            requirement=row["requirement"],
            source_clause=row.get("source_clause"),
            supplier_position=row.get("supplier_position"),
            status=row.get("status") or ComplianceStatus.OPEN,
            severity=row.get("severity"),
            action=row.get("action"),
            owner=row.get("owner"),
            resolved_at=(
                (before.get(row.get("id")) or now) if row.get("resolved") else None
            ),
        )
        for position, row in enumerate(rows)
    ]


def set_submission_fields(request: QuoteRequest, rows: list[dict[str, Any]]) -> None:
    """Replace the portal checklist, keeping when each cell was actually typed."""
    before = {row.id: row.entered_at for row in request.submission_fields}
    now = datetime.now(UTC)
    request.submission_fields = [
        QuoteSubmissionField(
            position=position,
            clause=row.get("clause"),
            label=row["label"],
            destination=row.get("destination"),
            value=row.get("value"),
            note=row.get("note"),
            is_mandatory=bool(row.get("is_mandatory")),
            entered_at=(
                (before.get(row.get("id")) or now) if row.get("entered") else None
            ),
        )
        for position, row in enumerate(rows)
    ]


def _decimal_or_none(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


#: Rates are stored to four places, so a markup produces a price rather than
#: whatever a multiplication happens to leave behind.
_RATE = Decimal("0.0001")

#: The quote's own ``name`` is a single line; a supplier's description can be a
#: paragraph. The rest goes to ``description`` rather than being cut off.
_NAME_LIMIT = 500


async def select_supplier(
    session: AsyncSession,
    request: QuoteRequest,
    *,
    user: User,
    supplier_quote_id: uuid.UUID,
    markup_percent: Decimal = Decimal(0),
) -> QuoteRequest:
    """Price the quote from one supplier's offer, line by line.

    This is the step that makes a quote submittable. Before it, the supplier
    quotes are an attachment — read, compared, sitting beside the request — and
    the request has no priced lines of its own, so there is nothing for an
    approver to approve.

    **What a supplier charges is a cost, not a price.** Their unit price becomes
    ``cost_rate`` and the selling ``rate`` is that plus the margin, so the two
    are never confused and the margin stays visible on every line while the
    quote is being reviewed. A markup of nothing is a real answer — it prices
    the job at cost — and every rate can be edited afterwards.

    The lines are replaced, not merged: choosing a supplier means taking their
    prices, and a merge of two suppliers' offers is not a quote from either.
    """
    require_editable(request, user=user)
    if request.comparison_id is None:
        raise QuoteError(
            "No supplier quotes are attached to this request yet, so there is "
            "nothing to price it from. Upload them first."
        )

    quote = await session.scalar(
        select(SupplierQuote).where(
            SupplierQuote.id == supplier_quote_id,
            # Scoped to this request's own comparison: a supplier quote from
            # somebody else's request is not an offer on this one.
            SupplierQuote.comparison_id == request.comparison_id,
        )
    )
    if quote is None:
        raise QuoteNotFoundError("That supplier quote is not attached to this request")
    if not quote.items:
        raise QuoteError(
            f"{quote.supplier_name} has no priced lines, so there is nothing to "
            f"quote from. Check what was read from their document."
        )

    set_items(request, [_line_from(item, quote, markup_percent) for item in quote.items])
    request.selected_supplier_quote_id = quote.id
    # Their prices are only half of what they sent. The terms they stated are
    # answers to the customer's own clauses, and carrying them across now is the
    # difference between a compliance matrix built from the document and one
    # built from somebody's memory of it a fortnight later.
    absorb_supplier(request, quote)
    seed_submission_checklist(request)
    if request.target_markup_percent is None and markup_percent:
        request.target_markup_percent = markup_percent
    await session.flush()
    logger.info(
        "quote %s priced from %s (%d lines, markup %s%%)",
        request.id,
        quote.supplier_name,
        len(quote.items),
        markup_percent,
    )
    return request


def _line_from(
    item: SupplierQuoteItem, quote: SupplierQuote, markup_percent: Decimal
) -> dict[str, Any]:
    """One supplier line as a line of ours, in the request's own currency."""
    # The comparison converted every supplier into one currency to compare them;
    # the same rate carries the cost across, or the totals would be a mixture of
    # currencies that happen to look like numbers.
    cost = ((item.unit_price or Decimal(0)) * (quote.fx_rate or Decimal(1))).quantize(_RATE)
    name = (item.description or "").strip() or "Unnamed line"
    return {
        "name": name[:_NAME_LIMIT],
        "description": name if len(name) > _NAME_LIMIT else None,
        "item_code": item.part_number,
        "brand": item.brand,
        "unit": item.unit,
        "quantity": item.quantity,
        "cost_rate": cost,
        "rate": (cost * (Decimal(1) + markup_percent / Decimal(100))).quantize(_RATE),
        "source_supplier_quote_id": quote.id,
    }


#: Refs for the compliance rows this module raises by itself, from the terms a
#: supplier stated on their own quotation. Prefixed so they are distinguishable
#: from the rows a person wrote, which are never touched when the supplier
#: changes. Stable, because they are matched on to avoid raising the same row
#: twice when a quote is repriced.
_SUPPLIER_REFS: Final = {
    "currency": "S-CUR",
    "validity": "S-VAL",
    "payment": "S-PAY",
    "warranty": "S-WAR",
    "incoterm": "S-INC",
    "delivery": "S-DEL",
}

#: Payment wording that means we pay before anybody pays us. Not a parser —
#: a list of the phrases suppliers actually use, and a miss costs nothing
#: because the row is raised either way; the match only decides whether it
#: arrives already marked as a deviation or as a question.
_PREPAYMENT_HINTS: Final = (
    "advance", "pre-payment", "prepayment", "proforma", "pro forma",
    "100% with order", "cash with order", "cwo", "before dispatch",
)

#: Wording that means there is no warranty. Deliberately narrow: a false
#: positive here marks a compliant offer as non-compliant, which is worse than
#: leaving a person to read the sentence themselves.
_NO_WARRANTY_HINTS: Final = (
    "no warranty", "not applicable", "n/a", "none", "not offered", "excluded",
)


def _mentions(text: str | None, hints: tuple[str, ...]) -> bool:
    lowered = (text or "").strip().lower()
    return bool(lowered) and any(hint in lowered for hint in hints)


def _incoterm_matches(required: str | None, offered: str | None) -> bool | None:
    """Whether the supplier's Incoterm is the one the RFP demands.

    ``None`` when either side is unstated, which is not the same as a mismatch —
    an unanswered question should arrive as a question.

    Compared on the three-letter term alone. "EXW Telford" and "EXW" are the
    same term and the place is a separate field; comparing the whole string
    would call every quote a deviation.
    """
    if not (required or "").strip() or not (offered or "").strip():
        return None
    return required.strip().split()[0].upper() == offered.strip().split()[0].upper()


def absorb_supplier(request: QuoteRequest, quote: SupplierQuote) -> None:
    """Carry everything the chosen supplier's quotation says onto the bid.

    A supplier quotation is not only prices. It states a currency, a validity, a
    payment term, a warranty position, an Incoterm and a lead time, and every
    one of those is a clause of the RFP that somebody has to answer. Retyping
    them into a compliance matrix by hand is how they get answered from memory
    two weeks later, so they are carried across the moment the supplier is
    chosen — as rows in the matrix, with the supplier's own words in them.

    Three rules keep this from being interference rather than help:

    * **Only blanks are filled.** A field somebody has already answered is left
      exactly as they answered it.
    * **Only rows this module raised are updated.** They are marked with their
      own refs; a row a person wrote is never rewritten when the supplier
      changes, because their judgement is not ours to overwrite.
    * **A status is only asserted where it can be derived.** Where the answer
      needs a human to read a sentence, the row arrives as an open question with
      the sentence in it rather than as a verdict that might be wrong.
    """
    # The money. Carried because every figure in the build-up depends on it,
    # and a bid costed at the wrong rate is wrong in one direction only.
    if quote.currency and quote.currency != request.currency:
        if not request.supplier_currency:
            request.supplier_currency = quote.currency
        if request.fx_rate is None and quote.fx_rate and quote.fx_rate > 0:
            request.fx_rate = quote.fx_rate

    # The manufacturer, when the supplier's lines agree on one. A specified-
    # brand line is won or lost on these matching the RFP character for
    # character, so they are worth carrying and never worth guessing at: if the
    # lines disagree, nothing is filled in.
    if not request.manufacturer_name:
        brands = {(i.brand or "").strip() for i in quote.items if (i.brand or "").strip()}
        if len(brands) == 1:
            request.manufacturer_name = brands.pop()[:200]
    if not request.manufacturer_part_number:
        parts = {
            (i.part_number or "").strip() for i in quote.items if (i.part_number or "").strip()
        }
        if len(parts) == 1:
            request.manufacturer_part_number = parts.pop()[:120]

    if not request.payment_terms and (quote.payment_terms or "").strip():
        request.payment_terms = quote.payment_terms.strip()[:200]
    if not request.delivery_terms and (quote.delivery_time or "").strip():
        request.delivery_terms = quote.delivery_time.strip()

    _seed_supplier_compliance(request, quote)


def _seed_supplier_compliance(request: QuoteRequest, quote: SupplierQuote) -> None:
    """One matrix row per term the supplier stated. See :func:`absorb_supplier`."""
    incoterm = _incoterm_matches(request.incoterm_required, quote.incoterms)
    who = quote.supplier_name

    proposed: list[dict[str, Any]] = []

    if quote.currency and quote.currency != request.currency:
        proposed.append(
            {
                "key": "currency",
                "area": ComplianceArea.COMMERCIAL,
                "requirement": f"Bid currency {request.currency}",
                "position": f"{who} quoted in {quote.currency}.",
                "status": ComplianceStatus.DEVIATION,
                "severity": Severity.MEDIUM,
                "action": (
                    f"Converted at the rate the bid is costed on. The exchange risk "
                    f"between {quote.currency} and {request.currency} is ours for as "
                    f"long as the bid stands."
                ),
            }
        )

    if (quote.validity or "").strip():
        proposed.append(
            {
                "key": "validity",
                "area": ComplianceArea.COMMERCIAL,
                "requirement": (
                    f"Bid validity {request.bid_validity_days} days"
                    if request.bid_validity_days
                    else "Bid validity as the RFP requires"
                ),
                "position": f"{who} states: {quote.validity.strip()}",
                "status": ComplianceStatus.OPEN,
                "severity": Severity.CRITICAL if request.bid_validity_days else None,
                "action": (
                    "Check this against the validity we are offering. Bidding a long "
                    "validity against a short supplier hold is an open position, not a "
                    "rounding difference — get it confirmed in writing before sending."
                ),
            }
        )

    if (quote.payment_terms or "").strip():
        early = _mentions(quote.payment_terms, _PREPAYMENT_HINTS)
        proposed.append(
            {
                "key": "payment",
                "area": ComplianceArea.COMMERCIAL,
                "requirement": "Payment terms as the customer's conditions require",
                "position": f"{who} requires: {quote.payment_terms.strip()}",
                "status": ComplianceStatus.DEVIATION if early else ComplianceStatus.OPEN,
                "severity": Severity.HIGH if early else None,
                "action": (
                    "We pay before we are paid. Price the cost of the money on the "
                    "landed-cost tab and declare the term — it is not ours to hide."
                    if early
                    else "Check against the customer's own payment conditions."
                ),
            }
        )

    if (quote.warranty or "").strip():
        none_offered = _mentions(quote.warranty, _NO_WARRANTY_HINTS)
        proposed.append(
            {
                "key": "warranty",
                "area": ComplianceArea.COMMERCIAL,
                "requirement": "Warranty as the customer's conditions require",
                "position": f"{who} states: {quote.warranty.strip()}",
                "status": (
                    ComplianceStatus.NON_COMPLIANT if none_offered else ComplianceStatus.OPEN
                ),
                "severity": Severity.HIGH if none_offered else None,
                "action": (
                    "Negotiate a warranty, or declare its absence as a deviation. A "
                    "warranty we have not been given is one we would be giving alone."
                    if none_offered
                    else "Check the term against what the customer asks for."
                ),
            }
        )

    if incoterm is not None:
        proposed.append(
            {
                "key": "incoterm",
                "area": ComplianceArea.LOGISTICS,
                "requirement": (
                    f"Incoterm {request.incoterm_required}"
                    + (f", {request.incoterm_place}" if request.incoterm_place else "")
                ),
                "position": f"{who} quoted {quote.incoterms.strip()}.",
                "status": (
                    ComplianceStatus.COMPLIANT if incoterm else ComplianceStatus.DEVIATION
                ),
                "severity": None if incoterm else Severity.HIGH,
                "action": (
                    None
                    if incoterm
                    else "The gap between the two terms is the freight, duty and "
                    "documentation. Build it on the landed-cost tab — this is usually "
                    "the largest single cost on a bid, and the easiest to forget."
                ),
            }
        )

    if (quote.delivery_time or "").strip():
        proposed.append(
            {
                "key": "delivery",
                "area": ComplianceArea.LOGISTICS,
                "requirement": "Delivery, in calendar days from the order",
                "position": f"{who} states: {quote.delivery_time.strip()}",
                "status": ComplianceStatus.OPEN,
                "severity": Severity.MEDIUM,
                "action": (
                    "Turn this into calendar days from the order and put it in the "
                    "delivery field. A supplier's working weeks are not the customer's "
                    "calendar days, and transit and clearance are on top of both."
                ),
            }
        )

    existing = {row.ref: row for row in request.compliance}
    position = len(request.compliance)
    for row in proposed:
        ref = _SUPPLIER_REFS[row["key"]]
        current = existing.get(ref)
        if current is None:
            request.compliance.append(
                QuoteComplianceItem(
                    position=position,
                    ref=ref,
                    area=row["area"],
                    requirement=row["requirement"],
                    source_clause=None,
                    supplier_position=row["position"],
                    status=row["status"],
                    severity=row["severity"],
                    action=row["action"],
                )
            )
            position += 1
            continue
        # The row is already there from an earlier supplier. Their words change;
        # the owner and whatever a person decided about it do not.
        current.supplier_position = row["position"]
        if current.resolved_at is None:
            current.status = row["status"]


def seed_submission_checklist(request: QuoteRequest) -> None:
    """The fields that get filled in wrong, as a checklist to work from.

    Only when the list is empty, so it never lands on top of somebody's work.

    These six are not a guess at what a portal asks for — every portal asks for
    something different. They are the ones that are decided *here*, on this
    screen, and then typed somewhere else by somebody reading from memory: the
    country of origin that gets left on our own country, the unit price that
    gets entered before the rounding was agreed, the part number that has to
    match the RFP character for character. The map from decision to cell is
    worth having precisely because the two live in different systems.
    """
    if request.submission_fields:
        return
    request.submission_fields = [
        QuoteSubmissionField(position=position, label=label, note=note, is_mandatory=True)
        for position, (label, note) in enumerate(
            (
                (
                    "Intend to respond",
                    "Portals default this to no. A bid on a line still set to no is "
                    "not a bid.",
                ),
                (
                    "Unit price",
                    "The rounded figure from the costing, not the computed one.",
                ),
                (
                    "Country of origin",
                    "Where the goods are made, which on a specified-brand line is "
                    "wherever the manufacturer makes them — not where we are.",
                ),
                ("Manufacturer name", "Exactly as the RFP spells it."),
                ("Manufacturer part number", "Exactly as the RFP spells it."),
                (
                    "Delivery, in calendar days",
                    "From the order, including payment clearance, production, transit "
                    "and customs.",
                ),
            )
        )
    ]


def markup_of(request: QuoteRequest) -> Decimal:
    """The margin already on this quote, as a percentage of its cost.

    Taken over the whole quote rather than per line, because that is what it is:
    one quote priced at one margin, whatever the individual lines do. Used when
    an approver switches supplier, so the quote is repriced at the margin the
    business chose rather than dropped to cost.
    """
    cost = sum(
        ((i.cost_rate or Decimal(0)) * (i.quantity or Decimal(0)) for i in request.items),
        Decimal(0),
    )
    if cost <= 0:
        return Decimal(0)
    sell = sum(
        ((i.rate or Decimal(0)) * (i.quantity or Decimal(0)) for i in request.items),
        Decimal(0),
    )
    return ((sell - cost) / cost * Decimal(100)).quantize(Decimal("0.01"))


async def create(
    session: AsyncSession, *, payload: dict[str, Any], author: User, team: Team
) -> QuoteRequest:
    request = QuoteRequest(
        title=payload.get("title") or "Untitled quote",
        customer_name=payload["customer_name"],
        # The objects, not their ids. Assigning ``team_id`` alone leaves ``team``
        # unloaded, and the response builder reading it back is then a lazy
        # SELECT from inside async code — a MissingGreenlet rather than a team.
        team=team,
        created_by=author,
        # Theirs until they hand it over. A rework comes back to a person.
        assigned_to=author,
        status=QuoteStatus.DRAFT,
        # Initialised so they count as loaded, for the same reason.
        items=[],
        reviews=[],
        comments=[],
        revisions=[],
        cost_lines=[],
        compliance=[],
        submission_fields=[],
    )
    apply_fields(request, payload)
    set_items(request, payload.get("items") or [])
    set_cost_lines(request, payload.get("cost_lines") or [])
    set_compliance(request, payload.get("compliance") or [])
    set_submission_fields(request, payload.get("submission_fields") or [])
    session.add(request)
    await session.flush()
    return request


#: Remarks and working notes are separate thoughts, so they read as separate
#: paragraphs rather than one run-on note.
_BLANK_LINE = chr(10) * 2


def _as_date(value: str | None) -> str | None:
    """A SharePoint timestamp as a plain date.

    The list stores dates as full timestamps at midnight UTC. A quote's bid
    closing date is a date, and carrying the time along only invites a timezone
    to shift it by a day.
    """
    return value[:10] if value else None


async def quotes_for_tasks(
    session: AsyncSession, task_ids: list[str]
) -> dict[str, QuoteRequest]:
    """The quotes already raised against these tasks, by task id.

    One query for the whole list rather than one per row: a person with two
    hundred tasks would otherwise wait for two hundred round trips to find out
    that three of them have quotes.
    """
    if not task_ids:
        return {}
    rows = await session.scalars(
        select(QuoteRequest).where(QuoteRequest.source_task_id.in_(task_ids))
    )
    return {r.source_task_id: r for r in rows if r.source_task_id}


#: An RFP or tender number as it appears in a Proposals title. The list is
#: typed by people, so this matches the shapes they actually use — "RFP
#: 6000149233", "Tender No: ABC/2026/44", "ITB-1234" — and gives up rather than
#: guessing when none of them fit. A wrong event number on a bid is worse than
#: an empty one: it is quoted back in every clarification and on the PO.
_RFP_PATTERN: Final = re.compile(
    r"\b(?:RFP|RFQ|ITB|ITT|TENDER|ENQUIRY|ENQ)\b[\s:.#/-]*"
    r"(?:NO\.?|NUMBER|#)?[\s:.#/-]*([A-Z0-9][A-Z0-9/._-]{3,})",
    re.IGNORECASE,
)


def rfp_number_in(title: str | None) -> str | None:
    """The buyer's event number out of a Proposals title, when it is in there."""
    if not title:
        return None
    found = _RFP_PATTERN.search(title)
    return found.group(1).strip(".:/-")[:120] if found else None


def payload_from_task(task: Any) -> dict[str, Any]:
    """A Proposals row as the beginnings of a quote request.

    Only what the list actually knows. Everything else — the lines, the terms,
    the prices — is the point of raising the quote and is not in the task.

    **The customer falls back to the task title when the row has no End User.**
    A fifth of that list is filled in loosely, and refusing to start a quote
    because a column is blank would push people back to raising them by hand.
    The win probability that follows carries its own basis, so an unknown
    customer reads as an unknown customer rather than as a confident number.

    On a tender the row knows more than an estimate has room for: who is buying,
    the event number in its own title, and the date they asked for. Those go to
    the bid pack. The buying entity and the customer are filled from the same
    column deliberately — on a tender they are usually the same organisation,
    and where they are not, the one that is wrong is the one somebody corrects,
    which is cheaper than the one nobody filled in.
    """
    notes = [
        f"{label}: {text.strip()}"
        for label, text in (("Remarks", task.remarks), ("Working notes", task.working_notes))
        if (text or "").strip()
    ]
    end_user = (task.end_user or "").strip()
    return {
        "title": task.title,
        "customer_name": end_user or task.title,
        # Zoho's own custom field for the bid closing date, which the Proposals
        # list has been calling BCD all along.
        "cf_bcd": _as_date(task.bid_closing_date),
        # Their Zoho quote number when the row already has one.
        "reference": (task.quote_no or "").strip()[:60] or None,
        "notes": _BLANK_LINE.join(notes) or None,
        # ── the bid pack's share of the row ────────────────────────────
        "rfp_number": rfp_number_in(task.title),
        "buying_entity": end_user or None,
        # Which portal the enquiry came through, which the list calls the type.
        "cf_portal": (task.current_type or "").strip()[:120] or None,
        # What the customer asked for, not what we will offer — the two are
        # different fields because on tenders the asked-for date has very often
        # already passed by the time the enquiry reaches anybody, and that gap
        # is itself a deviation somebody has to declare.
        "requested_delivery_date": _as_date(task.due_date),
    }


# ── the loop ───────────────────────────────────────────────────────────


def why_not_submit(request: QuoteRequest) -> str | None:
    """What stands between this quote and the approvers, if anything.

    One function rather than a list of checks in ``submit``, because the form
    needs the same answer *before* the button is pressed — a send button that
    only tells you what is missing after you press it is a guess with a delay.
    """
    if not request.items:
        return "A quote needs at least one line before it can be approved."
    if request.multiple_supplier_quotes and request.comparison_id is None:
        return (
            "This quote says several suppliers quoted, but no comparison is "
            "attached. Attach the supplier quotes, or turn the flag off."
        )
    if request.comparison_id is not None and request.selected_supplier_quote_id is None:
        return (
            "Supplier quotes are attached but none has been chosen. Choose the "
            "supplier this quote is priced from before sending it for approval."
        )
    return None


async def submit(session: AsyncSession, request: QuoteRequest, *, user: User) -> QuoteRequest:
    """Send it to the approvers."""
    require_editable(request, user=user)
    missing = why_not_submit(request)
    if missing is not None:
        raise QuoteError(missing)

    request.status = QuoteStatus.PENDING_APPROVAL
    request.submitted_at = datetime.now(UTC)
    await session.flush()
    return request


def snapshot_of(request: QuoteRequest) -> dict[str, Any]:
    """Everything a later reader needs about the round that is ending.

    Denormalised on purpose. A reviewer looking at round three wants to know
    what round two actually said, and a copy that joins back to the live tables
    would show them what those tables say *now* — which is the one thing it must
    not do.
    """
    return {
        "title": request.title,
        "customer_name": request.customer_name,
        "reference": request.reference,
        "reference_number": request.reference_number,
        "currency": request.currency,
        "status": str(request.status),
        "sub_total": str(request.sub_total),
        "total_excl_tax": str(request.total_excl_tax),
        "tax_total": str(request.tax_total),
        "total": str(request.total),
        "discount": str(request.discount or 0),
        "shipping_charge": str(request.shipping_charge or 0),
        "adjustment": str(request.adjustment or 0),
        "notes": request.notes,
        "terms": request.terms,
        "win_probability": (
            str(request.win_probability) if request.win_probability is not None else None
        ),
        "win_basis": request.win_basis,
        "selected_supplier_quote_id": (
            str(request.selected_supplier_quote_id)
            if request.selected_supplier_quote_id
            else None
        ),
        # The bid position as it stood. A negotiation is an argument about what
        # was offered before, and "we bid at 45% on a landed cost of X" is the
        # half of that argument a list of line items cannot carry.
        "bid": {
            "rfp_number": request.rfp_number,
            "line_item_ref": request.line_item_ref,
            "target_markup_percent": (
                str(request.target_markup_percent)
                if request.target_markup_percent is not None
                else None
            ),
            "submission_unit_price": (
                str(request.submission_unit_price)
                if request.submission_unit_price is not None
                else None
            ),
            "submission_total": (
                str(request.submission_total)
                if request.submission_total is not None
                else None
            ),
            "delivery_days": request.delivery_days,
            "country_of_origin": request.country_of_origin,
            "landed_total": str(bidpack.landed_cost(request).total),
            # What was still outstanding when the round ended, which is usually
            # the reason it ended the way it did.
            "open_issues": sum(1 for c in request.compliance if c.is_blocking),
        },
        "items": [
            {
                "position": item.position,
                "name": item.name,
                "description": item.description,
                "item_code": item.item_code,
                "brand": item.brand,
                "unit": item.unit,
                "quantity": str(item.quantity or 0),
                "rate": str(item.rate or 0),
                "discount": str(item.discount or 0),
                "cost_rate": str(item.cost_rate) if item.cost_rate is not None else None,
                "line_total": str(item.line_total),
                "margin": str(item.margin) if item.margin is not None else None,
                "source_supplier_quote_id": (
                    str(item.source_supplier_quote_id)
                    if item.source_supplier_quote_id
                    else None
                ),
            }
            for item in request.items
        ],
    }


def _keep_round(request: QuoteRequest, outcome: str) -> None:
    """Put the round that is ending into the history, once."""
    if any(r.revision == request.revision for r in request.revisions):
        return
    request.revisions.append(
        QuoteRevision(
            revision=request.revision,
            outcome=outcome,
            snapshot=snapshot_of(request),
        )
    )


async def open_negotiation(
    session: AsyncSession,
    request: QuoteRequest,
    *,
    user: User,
    roles: set[str],
    note: str,
) -> QuoteRequest:
    """The customer came back on an approved quote. Open another round.

    Only from **approved**: a quote that was rejected was not agreed to, and
    there is nothing to negotiate about — that is a new quote. A rejected one
    reopened here would also quietly re-enter the queue for Zoho.

    The round that was approved is already in the history, so the previous
    prices survive the repricing that follows. What the customer is asking for
    goes into the history too — a reopened quote with no reason attached leaves
    the next reviewer guessing at what changed and why.
    """
    # ``!=``, not ``is not``: status is a plain string column, so a quote read
    # back from the database carries a str rather than the enum member, and an
    # identity check would refuse every quote that had ever been saved.
    if request.status != QuoteStatus.APPROVED:
        raise QuoteError(
            f"This quote is {str(request.status).replace('_', ' ')}. Only an approved "
            f"quote can go back into negotiation; anything else is a new quote."
        )
    if not (note or "").strip():
        raise QuoteError("Reopening a quote needs to say what the customer is asking for.")

    mine = request.created_by_id == user.id or request.assigned_to_id == user.id
    allowed, reason = await may_approve(session, request, user=user, roles=roles)
    if not (mine or allowed):
        raise QuotePermissionError(
            "Only the people who own this quote or approve for the team can reopen it."
        )

    request.reviews.append(
        QuoteReview(
            reviewer=user,
            action=ReviewAction.NEGOTIATE,
            note=note.strip(),
            revision=request.revision,
            selected_supplier_quote_id=request.selected_supplier_quote_id,
        )
    )
    request.status = QuoteStatus.IN_NEGOTIATION
    # Back to a named person, as a rework is. A quote nobody owns is a quote
    # nobody reprices.
    request.assigned_to_id = request.assigned_to_id or request.created_by_id
    # Undecided again: the approval it had was of numbers that are about to
    # change, and a decided_at left behind would date a decision that no longer
    # stands.
    request.decided_at = None
    request.revision += 1
    await session.flush()
    logger.info("quote %s reopened for negotiation at r%d", request.id, request.revision)
    return request


async def review(
    session: AsyncSession,
    request: QuoteRequest,
    *,
    reviewer: User,
    roles: set[str],
    action: ReviewAction,
    note: str | None = None,
    selected_supplier_quote_id: uuid.UUID | None = None,
) -> QuoteReview:
    """Record a decision and move the request accordingly."""
    allowed, reason = await may_approve(session, request, user=reviewer, roles=roles)
    if not allowed:
        raise QuotePermissionError(reason)

    if action is not ReviewAction.COMMENT and request.status != QuoteStatus.PENDING_APPROVAL:
        raise QuoteError(
            f"This quote is {request.status.replace('_', ' ')}, so there is nothing "
            f"to decide. Only a quote awaiting approval can be approved, reworked "
            f"or rejected."
        )
    if action is ReviewAction.REJECT and not (note or "").strip():
        raise QuoteError("A rejection needs a reason.")
    if action is ReviewAction.REWORK and not (note or "").strip():
        raise QuoteError("Sending a quote back needs to say what should change.")

    if action is ReviewAction.APPROVE and request.multiple_supplier_quotes:
        chosen = selected_supplier_quote_id or request.selected_supplier_quote_id
        if chosen is None:
            raise QuoteError(
                "Several suppliers quoted, so approving means choosing one. "
                "Pass selected_supplier_quote_id."
            )
        quote = await session.get(SupplierQuote, chosen)
        if quote is None or quote.comparison_id != request.comparison_id:
            raise QuoteNotFoundError("That supplier quote is not attached to this request")

        if chosen != request.selected_supplier_quote_id:
            # The approver overruled the choice, so the lines have to follow.
            # Approving supplier B while the quote still carries supplier A's
            # prices would approve a document nobody wrote. Repriced at the
            # margin already on the quote rather than dropped to cost, and the
            # revision moves so that what was approved is visibly not what was
            # submitted.
            # What was submitted, before it was repriced. Kept first, or the
            # only surviving numbers would be the ones the approver made.
            _keep_round(request, "superseded")
            margin = markup_of(request)
            set_items(request, [_line_from(i, quote, margin) for i in quote.items])
            request.revision += 1
        request.selected_supplier_quote_id = chosen

    record = QuoteReview(
        # The object, not the id. The response names the reviewer, and an
        # unloaded ``reviewer`` makes that a lazy SELECT from inside
        # serialisation — a MissingGreenlet rather than a name.
        reviewer=reviewer,
        action=action,
        note=(note or "").strip() or None,
        revision=request.revision,
        selected_supplier_quote_id=request.selected_supplier_quote_id,
    )
    # Appended to the relationship rather than added to the session: the
    # collection is already loaded, and session.add would insert the row while
    # leaving the in-memory list stale — so the response would come back missing
    # the very decision that was just made.
    request.reviews.append(record)

    now = datetime.now(UTC)
    if action is not ReviewAction.COMMENT:
        # The round is over, so what it said is history from here. Taken before
        # the status moves, so a snapshot reads as the round it was.
        _keep_round(request, str(action))

    if action is ReviewAction.APPROVE:
        request.status = QuoteStatus.APPROVED
        request.decided_at = now
    elif action is ReviewAction.REJECT:
        request.status = QuoteStatus.REJECTED
        request.decided_at = now
    elif action is ReviewAction.REWORK:
        request.status = QuoteStatus.CHANGES_REQUESTED
        # Back to a named person, not into a pool nobody owns.
        request.assigned_to_id = request.assigned_to_id or request.created_by_id
        request.revision += 1
    # COMMENT deliberately moves nothing.

    await session.flush()
    return record


async def delete_request(
    session: AsyncSession, request: QuoteRequest, *, roles: set[str] | frozenset[str]
) -> None:
    """Remove a quote request outright. Super admin only.

    Deliberately not given to the person who raised it, and not given to
    approvers either. A quote carries an approval history — who agreed to what,
    and when — and the whole reason that history is appended and never edited is
    that somebody may need to answer for it months later. Letting the author
    delete the record of a decision they did not like would undo that in one
    click, and it would be the quotes most worth keeping that went.

    So this exists for the case it is actually needed for: a duplicate, a test
    row, something raised against the wrong customer. One person can do it, and
    it is the person who already administers the company.

    Everything hanging off the request goes with it — the lines, the reviews,
    the comments, the revisions, the landed-cost rows, the compliance matrix and
    the portal checklist. That is the cascade doing what it says, and it is
    right: half a deleted quote is worse than either outcome.

    **Not reversible, and nothing here is written to Zoho or SharePoint** — a
    quote deleted here was never in either.
    """
    if SUPER_ADMIN not in set(roles):
        raise QuotePermissionError(
            "Only a super admin can delete a quote request. A quote carries the "
            "record of who approved what, so it is not the author's to remove."
        )
    logger.warning(
        "quote %s (%s) deleted: %s",
        request.id,
        request.reference or request.title,
        request.status,
    )
    await session.delete(request)
    await session.flush()


# ── comments ───────────────────────────────────────────────────────────


async def comment(
    session: AsyncSession,
    request: QuoteRequest,
    *,
    author: User,
    body: str,
    target_type: CommentTarget = CommentTarget.QUOTE,
    target_ref: str | None = None,
) -> QuoteComment:
    """Anchor a remark to a field, a line, a supplier quote, or the whole thing."""
    if not body.strip():
        raise QuoteError("A comment needs something in it")
    if target_type in (CommentTarget.ITEM, CommentTarget.SUPPLIER_QUOTE) and not target_ref:
        raise QuoteError(f"A {target_type} comment must say which one")

    if target_type == CommentTarget.ITEM and not any(
        str(i.id) == target_ref for i in request.items
    ):
        raise QuoteNotFoundError("That line is not on this quote")

    row = QuoteComment(
        # The object, not the id — see the note in ``review``.
        author=author,
        target_type=target_type,
        target_ref=target_ref,
        body=body.strip(),
        revision=request.revision,
    )
    # Appended, not session.add — see the note in ``review``.
    request.comments.append(row)
    await session.flush()
    return row


async def resolve_comment(
    session: AsyncSession, row: QuoteComment, *, user: User
) -> QuoteComment:
    row.resolved_at = datetime.now(UTC)
    row.resolved_by_id = user.id
    await session.flush()
    return row


# ── reading ────────────────────────────────────────────────────────────


async def get(session: AsyncSession, request_id: uuid.UUID) -> QuoteRequest:
    request = await session.get(QuoteRequest, request_id)
    if request is None:
        raise QuoteNotFoundError("No such quote request")
    return request


async def approver_team_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """The teams whose quotes this person decides, by a role held inside them."""
    rows = await session.scalars(
        select(TeamMembership.team_id)
        .join(Role, Role.id == TeamMembership.role_id)
        .where(TeamMembership.user_id == user_id, Role.key.in_(TEAM_APPROVERS))
    )
    return set(rows.all())


async def listing(
    session: AsyncSession,
    *,
    team_id: uuid.UUID | None = None,
    status: QuoteStatus | None = None,
    mine_for: uuid.UUID | None = None,
    viewer: User | None = None,
    viewer_roles: set[str] | frozenset[str] = frozenset(),
    limit: int = 100,
) -> list[QuoteRequest]:
    """Quotes, newest first, narrowed to what ``viewer`` is allowed to see.

    A quote is somebody's negotiation with a customer, and the list of them is
    not a noticeboard. Three rules, and they are deliberately narrow:

    * **Your own** — raised by you, or handed to you.
    * **Waiting on you** — a quote actually *pending approval* in a team where
      you approve. Not that team's whole history: an approver needs to see what
      is waiting, which is a different question from what has ever been quoted.
      A team lead who happens to approve is not thereby an auditor of every
      draft and every rejection their colleagues have written.
    * **Everything** — for a super admin, the CEO or a manager, who answer for
      the business rather than for a quote.

    The narrowing matters more than it looks. These rows carry the cost behind
    every price, so the margin on a colleague's job is readable by anyone the
    list hands it to.

    With no ``viewer`` the list is unfiltered, for callers that have already
    settled who is asking.
    """
    query = select(QuoteRequest).order_by(QuoteRequest.created_at.desc()).limit(limit)
    if team_id is not None:
        query = query.where(QuoteRequest.team_id == team_id)
    if status is not None:
        query = query.where(QuoteRequest.status == status)
    if mine_for is not None:
        query = query.where(
            or_(
                QuoteRequest.created_by_id == mine_for,
                QuoteRequest.assigned_to_id == mine_for,
            )
        )
    if viewer is not None and not (set(viewer_roles) & GLOBAL_APPROVERS):
        visible = [
            QuoteRequest.created_by_id == viewer.id,
            QuoteRequest.assigned_to_id == viewer.id,
        ]
        decides = await approver_team_ids(session, viewer.id)
        if decides:
            # Waiting on them, rather than everything their team has ever
            # written. An approver needs the queue; the archive is a different
            # question and not one approving grants the right to ask.
            visible.append(
                and_(
                    QuoteRequest.team_id.in_(decides),
                    QuoteRequest.status == QuoteStatus.PENDING_APPROVAL,
                )
            )
        query = query.where(or_(*visible))
    return list((await session.scalars(query)).all())


async def queue(
    session: AsyncSession, *, team_id: uuid.UUID | None = None, assignee: uuid.UUID | None = None
) -> list[QuoteRequest]:
    """Approved and waiting to be created in Zoho.

    This is where the module stops. Nothing pushes to Zoho — the list is live and
    that step is deliberately not built yet.
    """
    query = (
        select(QuoteRequest)
        .where(QuoteRequest.status == QuoteStatus.APPROVED)
        .order_by(QuoteRequest.decided_at.asc())
    )
    if team_id is not None:
        query = query.where(QuoteRequest.team_id == team_id)
    if assignee is not None:
        query = query.where(QuoteRequest.assigned_to_id == assignee)
    return list((await session.scalars(query)).all())
