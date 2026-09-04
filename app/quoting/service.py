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
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comparison import SupplierQuote, SupplierQuoteItem
from app.models.quoting import (
    EDITABLE_STATUSES,
    CommentTarget,
    QuoteComment,
    QuoteRequest,
    QuoteRequestItem,
    QuoteReview,
    QuoteRevision,
    QuoteStatus,
    ReviewAction,
)
from app.models.role import Role, UserRole
from app.models.team import Team, TeamMembership
from app.models.user import User

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


def require_editable(request: QuoteRequest, *, user: User) -> None:
    if request.status not in EDITABLE_STATUSES:
        raise QuotePermissionError(
            f"This quote is {request.status.replace('_', ' ')} and cannot be edited. "
            f"Editing it while approvers are looking would mean they approved "
            f"something that no longer exists."
        )
    if request.created_by_id != user.id and request.assigned_to_id != user.id:
        raise QuotePermissionError("This quote is not yours to edit.")


# ── building one ───────────────────────────────────────────────────────


def apply_fields(request: QuoteRequest, payload: dict[str, Any]) -> None:
    """Copy the Zoho-shaped form fields onto the row."""
    for field in (
        "title", "customer_name", "customer_id", "contact_person", "reference_number",
        "quote_date", "expiry_date", "currency", "salesperson_name", "place_of_supply",
        "payment_terms", "delivery_terms", "cf_bcd", "cf_portal", "subject", "notes",
        "terms", "reference",
    ):
        if field in payload:
            setattr(request, field, payload[field])
    for field in ("discount", "shipping_charge", "adjustment"):
        if payload.get(field) is not None:
            setattr(request, field, Decimal(str(payload[field])))
    if payload.get("multiple_supplier_quotes") is not None:
        request.multiple_supplier_quotes = bool(payload["multiple_supplier_quotes"])


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
    )
    apply_fields(request, payload)
    set_items(request, payload.get("items") or [])
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


def payload_from_task(task: Any) -> dict[str, Any]:
    """A Proposals row as the beginnings of a quote request.

    Only what the list actually knows. Everything else — the lines, the terms,
    the prices — is the point of raising the quote and is not in the task.

    **The customer falls back to the task title when the row has no End User.**
    A fifth of that list is filled in loosely, and refusing to start a quote
    because a column is blank would push people back to raising them by hand.
    The win probability that follows carries its own basis, so an unknown
    customer reads as an unknown customer rather than as a confident number.
    """
    notes = [
        f"{label}: {text.strip()}"
        for label, text in (("Remarks", task.remarks), ("Working notes", task.working_notes))
        if (text or "").strip()
    ]
    return {
        "title": task.title,
        "customer_name": (task.end_user or "").strip() or task.title,
        # Zoho's own custom field for the bid closing date, which the Proposals
        # list has been calling BCD all along.
        "cf_bcd": _as_date(task.bid_closing_date),
        # Their Zoho quote number when the row already has one.
        "reference": (task.quote_no or "").strip()[:60] or None,
        "notes": _BLANK_LINE.join(notes) or None,
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


async def listing(
    session: AsyncSession,
    *,
    team_id: uuid.UUID | None = None,
    status: QuoteStatus | None = None,
    mine_for: uuid.UUID | None = None,
    limit: int = 100,
) -> list[QuoteRequest]:
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
