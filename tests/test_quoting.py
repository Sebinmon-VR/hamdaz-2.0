"""Quote requests and the approval loop.

Nothing here touches Zoho. The module stops at the queue, and these tests prove
it stops there rather than trusting a comment that says so.

The cases worth attacking are the ones a plausible implementation gets wrong in
a way nobody notices until it matters: editing a quote while approvers are
looking at it, somebody approving their own work, and a rework vanishing into a
pool with no owner.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.models.comparison import (
    ComparisonStatus,
    QuoteComparison,
    SupplierQuote,
    SupplierQuoteItem,
)
from app.models.quoting import CommentTarget, QuoteStatus, ReviewAction
from app.quoting import service
from app.quoting.service import QuoteError, QuoteNotFoundError, QuotePermissionError
from app.roles import service as roles_service
from app.teams import service as teams


async def person(db, email: str, *global_roles: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in global_roles:
        await roles_service.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)
    await db.commit()
    return user


@pytest.fixture
async def team(db):
    await roles_service.seed_system_roles(db)
    t = await teams.create_team(db, name="Presales")
    await db.commit()
    return t


@pytest.fixture
async def requester(db, team):
    user = await person(db, "engineer@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()
    return user


@pytest.fixture
async def approver(db, team):
    user = await person(db, "approver@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=user, role_keys=["approver"])
    await db.commit()
    return user


FORM = {
    "title": "Firewall refresh",
    "customer_name": "ADNOC",
    "reference_number": "MR-TRJ-24-01-0790",
    "currency": "AED",
    "items": [
        {"name": "FortiGate 201G", "quantity": 2, "rate": 12000, "cost_rate": 9000},
        {"name": "3yr subscription", "quantity": 2, "rate": 4000, "cost_rate": 3100},
    ],
}


async def draft(db, requester, team, **over):
    return await service.create(
        db, payload={**FORM, **over}, author=requester, team=team
    )


async def with_suppliers(db, request, requester, *, rates=(1000, 900), fx=1):
    """Attach a comparison holding one supplier quote per rate.

    Built directly rather than through the upload route: what these tests are
    about is what happens to a quote once supplier lines exist, not how the
    documents were read.
    """
    comparison = QuoteComparison(
        title=f"Supplier quotes for {request.title}",
        currency="AED",
        status=ComparisonStatus.SAVED,
        created_by=requester,
        quotes=[
            SupplierQuote(
                supplier_name=f"Supplier {chr(65 + i)}",
                currency="AED",
                fx_rate=Decimal(str(fx)),
                items=[
                    SupplierQuoteItem(
                        position=0,
                        description="FortiGate 201G",
                        part_number="FG-201G",
                        brand="Fortinet",
                        unit="each",
                        quantity=Decimal(2),
                        unit_price=Decimal(str(rate)),
                    ),
                    SupplierQuoteItem(
                        position=1,
                        description="3yr subscription",
                        part_number="FC-3Y",
                        quantity=Decimal(2),
                        unit_price=Decimal(str(rate)) / 4,
                    ),
                ],
            )
            for i, rate in enumerate(rates)
        ],
    )
    db.add(comparison)
    await db.flush()
    request.comparison = comparison
    request.multiple_supplier_quotes = True
    await db.flush()
    return comparison


# ── the form ───────────────────────────────────────────────────────────


async def test_a_new_quote_starts_as_a_draft_owned_by_its_author(db, requester, team) -> None:
    """Owned, not pooled — a rework has to come back to somebody."""
    request = await draft(db, requester, team)
    await db.commit()

    assert request.status == QuoteStatus.DRAFT
    assert request.assigned_to_id == requester.id
    assert request.revision == 1


async def test_the_total_is_computed_not_accepted(db, requester, team) -> None:
    """A caller could otherwise post whatever total suited them."""
    request = await draft(db, requester, team, discount=1000, shipping_charge=250)
    await db.commit()

    assert request.sub_total == Decimal(2 * 12000 + 2 * 4000)
    assert request.total == Decimal(32000 - 1000 + 250)


async def test_margin_is_visible_where_a_cost_is_known(db, requester, team) -> None:
    """Reviewing a price without the cost behind it is reviewing half of it."""
    request = await draft(db, requester, team)
    await db.commit()

    assert request.items[0].margin == Decimal((12000 - 9000) * 2)


async def test_a_line_with_no_cost_reports_no_margin(db, requester, team) -> None:
    """None, not zero: unknown and break-even are different facts."""
    request = await draft(
        db, requester, team, items=[{"name": "Labour", "quantity": 1, "rate": 500}]
    )
    await db.commit()

    assert request.items[0].margin is None


# ── submitting locks it ────────────────────────────────────────────────


async def test_submitting_sends_it_to_the_approvers(db, requester, team) -> None:
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)
    await db.commit()

    assert request.status == QuoteStatus.PENDING_APPROVAL
    assert request.submitted_at is not None


async def test_a_quote_with_no_lines_cannot_be_submitted(db, requester, team) -> None:
    request = await draft(db, requester, team, items=[])
    with pytest.raises(QuoteError, match="at least one line"):
        await service.submit(db, request, user=requester)


async def test_it_cannot_be_edited_while_approvers_are_looking(db, requester, team) -> None:
    """The failure this module exists to prevent: approving something that has
    since changed."""
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)
    await db.commit()

    with pytest.raises(QuotePermissionError, match="cannot be edited"):
        service.require_editable(request, user=requester)


async def test_somebody_elses_quote_cannot_be_edited(db, requester, team) -> None:
    request = await draft(db, requester, team)
    await db.commit()
    other = await person(db, "someone@hamdaz.com")

    with pytest.raises(QuotePermissionError, match="not yours"):
        service.require_editable(request, user=other)


async def test_claiming_several_suppliers_without_attaching_any_is_refused(
    db, requester, team
) -> None:
    """The flag drives the comparison and the 'which one won' decision, so it
    cannot be true with nothing behind it."""
    request = await draft(db, requester, team, multiple_supplier_quotes=True)
    with pytest.raises(QuoteError, match="no comparison is attached"):
        await service.submit(db, request, user=requester)


# ── who may decide ─────────────────────────────────────────────────────


async def test_an_approver_of_the_team_may_decide(db, requester, approver, team) -> None:
    request = await draft(db, requester, team)
    await db.commit()

    allowed, _ = await service.may_approve(db, request, user=approver, roles=set())
    assert allowed


async def test_nobody_approves_their_own_quote(db, requester, team) -> None:
    """Not a hierarchy question. A quote reviewed only by its author has not
    been reviewed."""
    request = await draft(db, requester, team)
    await teams.set_member_roles(db, team=team, user=requester, role_keys=["approver"])
    await db.commit()

    allowed, reason = await service.may_approve(db, request, user=requester, roles=set())
    assert not allowed
    assert "raised yourself" in reason


async def test_an_ordinary_member_may_not_decide(db, requester, team) -> None:
    other = await person(db, "member2@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=other, role_keys=["member"])
    await db.commit()
    request = await draft(db, requester, team)

    allowed, reason = await service.may_approve(db, request, user=other, roles=set())
    assert not allowed
    assert "approver, team lead or manager" in reason


async def test_a_manager_may_decide_without_being_on_the_team(db, requester, team) -> None:
    manager = await person(db, "mgr@hamdaz.com", "manager")
    request = await draft(db, requester, team)
    await db.commit()

    allowed, _ = await service.may_approve(db, request, user=manager, roles={"manager"})
    assert allowed


# ── the loop ───────────────────────────────────────────────────────────


async def test_approving_puts_it_in_the_queue(db, requester, approver, team) -> None:
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)
    await service.review(
        db, request, reviewer=approver, roles=set(), action=ReviewAction.APPROVE
    )
    await db.commit()

    assert request.status == QuoteStatus.APPROVED
    assert request.decided_at is not None
    assert request in await service.queue(db, team_id=team.id)


async def test_a_rework_goes_back_to_a_named_person_and_bumps_the_revision(
    db, requester, approver, team
) -> None:
    """Back to somebody, never into a pool nobody owns."""
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)
    await service.review(
        db, request, reviewer=approver, roles=set(),
        action=ReviewAction.REWORK, note="Margin on line 2 is too thin.",
    )
    await db.commit()

    assert request.status == QuoteStatus.CHANGES_REQUESTED
    assert request.assigned_to_id == requester.id
    assert request.revision == 2
    # And it is editable again.
    service.require_editable(request, user=requester)


async def test_the_loop_can_go_round_again(db, requester, approver, team) -> None:
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)
    await service.review(db, request, reviewer=approver, roles=set(),
                         action=ReviewAction.REWORK, note="Fix the rates.")
    await service.submit(db, request, user=requester)
    await service.review(db, request, reviewer=approver, roles=set(),
                         action=ReviewAction.APPROVE)
    await db.commit()

    assert request.status == QuoteStatus.APPROVED
    assert request.revision == 2
    assert [r.action for r in request.reviews] == [ReviewAction.REWORK, ReviewAction.APPROVE]


async def test_a_rejection_needs_a_reason(db, requester, approver, team) -> None:
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)

    with pytest.raises(QuoteError, match="needs a reason"):
        await service.review(db, request, reviewer=approver, roles=set(),
                             action=ReviewAction.REJECT)


async def test_a_rework_must_say_what_to_change(db, requester, approver, team) -> None:
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)

    with pytest.raises(QuoteError, match="what should change"):
        await service.review(db, request, reviewer=approver, roles=set(),
                             action=ReviewAction.REWORK)


async def test_a_comment_decides_nothing(db, requester, approver, team) -> None:
    """It must not quietly move the quote out of review."""
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)
    await service.review(db, request, reviewer=approver, roles=set(),
                         action=ReviewAction.COMMENT, note="Who is the end user?")
    await db.commit()

    assert request.status == QuoteStatus.PENDING_APPROVAL


async def test_an_already_decided_quote_cannot_be_decided_again(
    db, requester, approver, team
) -> None:
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)
    await service.review(db, request, reviewer=approver, roles=set(),
                         action=ReviewAction.APPROVE)
    await db.commit()

    with pytest.raises(QuoteError, match="nothing to decide"):
        await service.review(db, request, reviewer=approver, roles=set(),
                             action=ReviewAction.REJECT, note="changed my mind")


async def test_the_review_history_is_appended_not_rewritten(
    db, requester, approver, team
) -> None:
    """An approval history that can be edited is not a history."""
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)
    await service.review(db, request, reviewer=approver, roles=set(),
                         action=ReviewAction.REWORK, note="one")
    await service.submit(db, request, user=requester)
    await service.review(db, request, reviewer=approver, roles=set(),
                         action=ReviewAction.REWORK, note="two")
    await db.commit()

    assert [(r.revision, r.note) for r in request.reviews] == [(1, "one"), (2, "two")]


# ── comments, anchored to things ───────────────────────────────────────


async def test_a_comment_can_be_anchored_to_a_line(db, requester, approver, team) -> None:
    """On the rate, not floating at the bottom of the page."""
    request = await draft(db, requester, team)
    await db.commit()

    row = await service.comment(
        db, request, author=approver, body="This rate looks high.",
        target_type=CommentTarget.ITEM, target_ref=str(request.items[0].id),
    )
    await db.commit()

    assert row.target_type == CommentTarget.ITEM
    assert row.is_open


async def test_a_comment_on_a_line_that_is_not_there_is_refused(
    db, requester, approver, team
) -> None:
    request = await draft(db, requester, team)
    await db.commit()

    with pytest.raises(QuoteError, match="not on this quote"):
        await service.comment(
            db, request, author=approver, body="?",
            target_type=CommentTarget.ITEM,
            target_ref="00000000-0000-0000-0000-000000000000",
        )


async def test_a_comment_records_the_round_it_was_written_in(
    db, requester, approver, team
) -> None:
    """So an old note is visibly old rather than a live objection."""
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)
    await service.review(db, request, reviewer=approver, roles=set(),
                         action=ReviewAction.REWORK, note="redo")
    row = await service.comment(db, request, author=approver, body="second round")
    await db.commit()

    assert row.revision == 2


async def test_resolving_keeps_the_comment(db, requester, approver, team) -> None:
    """The exchange is the record of why the quote ended up as it did."""
    request = await draft(db, requester, team)
    row = await service.comment(db, request, author=approver, body="check this")
    await service.resolve_comment(db, row, user=requester)
    await db.commit()

    assert not row.is_open
    assert row.body == "check this"


# ── the queue, and where this stops ────────────────────────────────────


async def test_only_approved_quotes_reach_the_queue(db, requester, approver, team) -> None:
    pending = await draft(db, requester, team)
    await service.submit(db, pending, user=requester)
    approved = await draft(db, requester, team, title="Second")
    await service.submit(db, approved, user=requester)
    await service.review(db, approved, reviewer=approver, roles=set(),
                         action=ReviewAction.APPROVE)
    await db.commit()

    queued = await service.queue(db, team_id=team.id)
    assert [q.title for q in queued] == ["Second"]


async def test_a_rejected_quote_never_reaches_the_queue(db, requester, approver, team) -> None:
    request = await draft(db, requester, team)
    await service.submit(db, request, user=requester)
    await service.review(db, request, reviewer=approver, roles=set(),
                         action=ReviewAction.REJECT, note="Customer withdrew.")
    await db.commit()

    assert await service.queue(db, team_id=team.id) == []


# ── pricing the quote from a supplier ──────────────────────────────────


async def test_choosing_a_supplier_prices_the_quote_from_their_lines(
    db, requester, team
) -> None:
    """The step that gives a quote something of its own to approve."""
    request = await draft(db, requester, team)
    comparison = await with_suppliers(db, request, requester)
    cheaper = comparison.quotes[1]

    await service.select_supplier(
        db, request, user=requester, supplier_quote_id=cheaper.id, margin_percent=Decimal(20)
    )

    assert [i.name for i in request.items] == ["FortiGate 201G", "3yr subscription"]
    # What the supplier charges is the cost...
    assert request.items[0].cost_rate == Decimal("900.0000")
    # ...and the price keeps a fifth of itself as margin: 900 ÷ 0.8, not
    # 900 × 1.2. The margin stays visible on the line.
    assert request.items[0].rate == Decimal("1125.0000")
    assert request.items[0].margin == Decimal("450.0000")
    assert request.items[0].source_supplier_quote_id == cheaper.id
    assert request.selected_supplier_quote_id == cheaper.id


async def test_no_margin_prices_the_job_at_cost(db, requester, team) -> None:
    """A real answer, and a visible one — not a quote with no prices in it."""
    request = await draft(db, requester, team)
    comparison = await with_suppliers(db, request, requester)

    await service.select_supplier(
        db, request, user=requester, supplier_quote_id=comparison.quotes[0].id
    )

    assert request.items[0].rate == request.items[0].cost_rate
    assert request.items[0].margin == 0


async def test_the_quote_takes_the_currency_of_the_supplier_it_is_priced_from(
    db, requester, team
) -> None:
    """A dollar offer makes a dollar quote: their price is the cost exactly as
    they wrote it, not converted into whatever the quote happened to be in."""
    request = await draft(db, requester, team)
    comparison = await with_suppliers(db, request, requester, rates=(100,))
    comparison.quotes[0].currency = "USD"
    assert request.currency != "USD"

    await service.select_supplier(
        db, request, user=requester, supplier_quote_id=comparison.quotes[0].id,
        margin_percent=Decimal(20),
    )

    assert request.currency == "USD"
    assert request.fx_rate is None
    assert request.items[0].cost_rate == Decimal("100.0000")
    assert request.items[0].rate == Decimal("125.0000")   # 100 ÷ 0.8, to the cent


async def test_choosing_a_different_supplier_replaces_the_lines(
    db, requester, team
) -> None:
    """A merge of two suppliers' offers is not a quote from either."""
    request = await draft(db, requester, team)
    comparison = await with_suppliers(db, request, requester)
    await service.select_supplier(
        db, request, user=requester, supplier_quote_id=comparison.quotes[0].id
    )

    await service.select_supplier(
        db, request, user=requester, supplier_quote_id=comparison.quotes[1].id
    )

    assert len(request.items) == 2
    assert {i.source_supplier_quote_id for i in request.items} == {comparison.quotes[1].id}


async def test_a_supplier_quote_from_another_request_cannot_be_chosen(
    db, requester, team
) -> None:
    mine = await draft(db, requester, team)
    await with_suppliers(db, mine, requester)
    theirs = await draft(db, requester, team, title="Somebody else's")
    other = await with_suppliers(db, theirs, requester)

    with pytest.raises(QuoteError):
        await service.select_supplier(
            db, mine, user=requester, supplier_quote_id=other.quotes[0].id
        )


async def test_a_quote_with_suppliers_attached_waits_for_one_to_be_chosen(
    db, requester, team
) -> None:
    """The gate on sending for approval, and the reason a form can show why."""
    request = await draft(db, requester, team)
    comparison = await with_suppliers(db, request, requester)

    assert "none has been chosen" in service.why_not_submit(request)
    with pytest.raises(QuoteError):
        await service.submit(db, request, user=requester)

    await service.select_supplier(
        db, request, user=requester, supplier_quote_id=comparison.quotes[0].id
    )

    assert service.why_not_submit(request) is None
    await service.submit(db, request, user=requester)
    assert request.status == QuoteStatus.PENDING_APPROVAL


async def test_an_approver_choosing_another_supplier_reprices_the_quote(
    db, requester, approver, team
) -> None:
    """Approving supplier B on supplier A's prices would approve nobody's quote."""
    request = await draft(db, requester, team)
    comparison = await with_suppliers(db, request, requester)
    await service.select_supplier(
        db, request, user=requester, supplier_quote_id=comparison.quotes[0].id,
        margin_percent=Decimal(20),
    )
    await service.submit(db, request, user=requester)
    was = request.revision

    await service.review(
        db,
        request,
        reviewer=approver,
        roles=set(),
        action=ReviewAction.APPROVE,
        selected_supplier_quote_id=comparison.quotes[1].id,
    )

    assert request.status == QuoteStatus.APPROVED
    assert request.selected_supplier_quote_id == comparison.quotes[1].id
    # Repriced from the cheaper supplier, at the margin the business chose.
    assert request.items[0].cost_rate == Decimal("900.0000")
    assert request.items[0].rate == Decimal("1125.0000")
    # And what was approved is visibly not what was submitted.
    assert request.revision == was + 1


# ── raising one from a task ────────────────────────────────────────────


class Task:
    """A Proposals row, as ``payload_from_task`` reads one."""

    def __init__(self, **over):
        self.id = "412"
        self.title = "MR-TRJ-24-01-0790 Firewall refresh"
        self.end_user = "ADNOC Onshore"
        self.bid_closing_date = "2026-09-30T00:00:00Z"
        self.quote_no = "QT-00218"
        self.remarks = "Budgetary only at this stage."
        self.working_notes = "Waiting on Fortinet pricing."
        self.web_url = "https://hamdaz1.sharepoint.com/Lists/Proposals/412"
        self.has_attachments = False
        self.attachments_url = None
        # The columns the bid pack reads. Present on every real row; they were
        # missing here only because nothing used to look at them.
        self.current_type = "Ariba"
        self.due_date = "2026-11-01T00:00:00Z"
        self.__dict__.update(over)


def test_a_task_fills_in_what_the_list_knows() -> None:
    form = service.payload_from_task(Task())

    assert form["title"] == "MR-TRJ-24-01-0790 Firewall refresh"
    assert form["customer_name"] == "ADNOC Onshore"
    # The bid closing date, as a date. A timestamp here invites a timezone to
    # move it by a day.
    assert form["cf_bcd"] == "2026-09-30"
    assert form["reference"] == "QT-00218"
    assert "Budgetary only" in form["notes"]
    assert "Waiting on Fortinet" in form["notes"]


def test_a_task_starts_the_bid_pack_off_as_well() -> None:
    """What the list knows that an estimate has no room for.

    The buying entity comes from the same column as the customer, deliberately:
    on a tender they are usually the same organisation, and where they are not,
    a wrong one somebody corrects beats an empty one nobody notices.
    """
    form = service.payload_from_task(Task())

    assert form["buying_entity"] == "ADNOC Onshore"
    assert form["cf_portal"] == "Ariba"
    # The date the customer asked for — kept apart from what we will offer,
    # because on a tender the gap between them is a deviation to declare.
    assert form["requested_delivery_date"] == "2026-11-01"


def test_an_event_number_is_taken_out_of_the_title_when_it_is_in_there() -> None:
    """A wrong event number is worse than none — it is quoted back everywhere."""
    assert service.rfp_number_in("RFP 6000149233 — CLOTH") == "6000149233"
    assert service.rfp_number_in("Tender No: ABC/2026/44 pumps") == "ABC/2026/44"
    assert service.rfp_number_in("Supply of valves") is None


def test_a_task_with_no_end_user_still_starts_a_quote() -> None:
    """A blank column is not a reason to send somebody back to typing it out."""
    form = service.payload_from_task(Task(end_user=None))

    assert form["customer_name"] == "MR-TRJ-24-01-0790 Firewall refresh"


def test_a_task_with_nothing_written_on_it_leaves_the_notes_empty() -> None:
    form = service.payload_from_task(Task(remarks="", working_notes=None))

    assert form["notes"] is None


# ── negotiation ────────────────────────────────────────────────────────


async def approved(db, requester, approver, team):
    """A quote that has been through a round and come out approved."""
    request = await draft(db, requester, team)
    comparison = await with_suppliers(db, request, requester)
    await service.select_supplier(
        db, request, user=requester, supplier_quote_id=comparison.quotes[0].id,
        margin_percent=Decimal(20),
    )
    request.win_probability = Decimal("0.4200")
    await service.submit(db, request, user=requester)
    await service.review(
        db, request, reviewer=approver, roles=set(), action=ReviewAction.APPROVE
    )
    return request, comparison


async def test_a_decision_keeps_the_round_it_decided(db, requester, approver, team) -> None:
    """Without this the previous prices are gone and a negotiation has no subject."""
    request, _ = await approved(db, requester, approver, team)

    assert len(request.revisions) == 1
    kept = request.revisions[0]
    assert kept.revision == 1
    assert kept.outcome == "approve"
    assert Decimal(kept.snapshot["items"][0]["rate"]) == Decimal(1250)
    assert kept.snapshot["win_probability"] == "0.4200"
    assert kept.snapshot["total"] == str(request.total)


async def test_a_negotiation_reopens_the_quote_and_keeps_what_was_quoted(
    db, requester, approver, team
) -> None:
    request, comparison = await approved(db, requester, approver, team)
    was = request.total

    await service.open_negotiation(
        db, request, user=requester, roles=set(),
        note="Customer wants 8% off and delivery in three weeks.",
    )

    assert request.status == QuoteStatus.IN_NEGOTIATION
    assert request.revision == 2
    # Undecided again: the approval it had was of numbers about to change.
    assert request.decided_at is None
    assert request.is_editable
    # The ask is in the history, beside the round it is about.
    assert request.reviews[-1].action == ReviewAction.NEGOTIATE
    assert "8% off" in request.reviews[-1].note
    # And the approved round is still readable in full.
    assert request.revisions[0].snapshot["total"] == str(was)


async def test_the_reprice_after_a_negotiation_leaves_the_old_one_standing(
    db, requester, approver, team
) -> None:
    """What the reviewer of round two needs: both sets of numbers."""
    request, comparison = await approved(db, requester, approver, team)
    await service.open_negotiation(
        db, request, user=requester, roles=set(), note="Wants it cheaper."
    )

    await service.select_supplier(
        db, request, user=requester, supplier_quote_id=comparison.quotes[1].id,
        margin_percent=Decimal(10),
    )
    await service.submit(db, request, user=requester)

    assert request.items[0].rate == Decimal("1000.0000")
    assert Decimal(request.revisions[0].snapshot["items"][0]["rate"]) == Decimal(1250)
    assert Decimal(request.revisions[0].snapshot["total"]) > request.total


async def test_only_an_approved_quote_can_be_negotiated(db, requester, team) -> None:
    """A rejected quote was not agreed to, so there is nothing to negotiate."""
    request = await draft(db, requester, team)

    with pytest.raises(QuoteError):
        await service.open_negotiation(
            db, request, user=requester, roles=set(), note="They want a discount."
        )


async def test_reopening_a_quote_has_to_say_why(db, requester, approver, team) -> None:
    request, _ = await approved(db, requester, approver, team)

    with pytest.raises(QuoteError):
        await service.open_negotiation(db, request, user=requester, roles=set(), note="  ")


async def test_a_stranger_cannot_reopen_somebody_elses_quote(
    db, requester, approver, team
) -> None:
    request, _ = await approved(db, requester, approver, team)
    outsider = await person(db, "nobody@hamdaz.com")

    with pytest.raises(QuotePermissionError):
        await service.open_negotiation(
            db, request, user=outsider, roles=set(), note="Let me at it."
        )


async def test_an_approver_switching_supplier_keeps_both_sets_of_numbers(
    db, requester, approver, team
) -> None:
    """The submitted round and the approved one are different documents."""
    request = await draft(db, requester, team)
    comparison = await with_suppliers(db, request, requester)
    await service.select_supplier(
        db, request, user=requester, supplier_quote_id=comparison.quotes[0].id,
        margin_percent=Decimal(20),
    )
    await service.submit(db, request, user=requester)

    await service.review(
        db, request, reviewer=approver, roles=set(), action=ReviewAction.APPROVE,
        selected_supplier_quote_id=comparison.quotes[1].id,
    )

    outcomes = {r.outcome: r for r in request.revisions}
    assert set(outcomes) == {"superseded", "approve"}
    # What was sent up...
    assert Decimal(outcomes["superseded"].snapshot["items"][0]["rate"]) == Decimal(1250)
    # ...and what was actually approved.
    assert Decimal(outcomes["approve"].snapshot["items"][0]["rate"]) == Decimal(1125)

# ── who may delete one ─────────────────────────────────────────────────


async def test_only_a_super_admin_can_delete_a_quote(db, team, requester, approver) -> None:
    """Not the author's, on purpose.

    A quote carries an approval history whose whole value is that it cannot be
    rewritten. Letting the person who raised it delete the record of a decision
    they disliked would undo that in one click — and it would be the quotes most
    worth keeping that went.
    """
    request = await service.create(db, payload=dict(FORM), author=requester, team=team)
    await db.commit()

    for roles in ({"approver"}, {"manager"}, {"ceo"}, set()):
        with pytest.raises(QuotePermissionError):
            await service.delete_request(db, request, roles=roles)


async def test_a_super_admin_deletes_the_quote_and_everything_under_it(
    db, team, requester
) -> None:
    request = await service.create(db, payload=dict(FORM), author=requester, team=team)
    await service.comment(db, request, author=requester, body="a remark")
    await db.commit()
    request_id = request.id

    await service.delete_request(db, request, roles={"super_admin"})
    await db.commit()

    with pytest.raises(QuoteNotFoundError):
        await service.get(db, request_id)


# ── who sees which quotes ──────────────────────────────────────────────


async def test_the_list_shows_a_person_their_own_and_nobody_elses(
    db, team, requester
) -> None:
    other = await person(db, "colleague@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=other, role_keys=["member"])
    await service.create(
        db, payload={**FORM, "title": "Mine"}, author=requester, team=team
    )
    await service.create(
        db, payload={**FORM, "title": "Theirs"}, author=other, team=team
    )
    await db.commit()

    titles = {r.title for r in await service.listing(db, viewer=requester, viewer_roles=set())}
    assert titles == {"Mine"}


async def test_an_approver_sees_what_is_waiting_not_the_teams_archive(
    db, team, requester, approver
) -> None:
    """Approving is not the same as auditing.

    A team lead who approves should see the queue, which is what they have to
    act on. A colleague's untouched draft carries the cost behind every price
    and is not theirs to read.
    """
    draft = await service.create(
        db, payload={**FORM, "title": "Still a draft"}, author=requester, team=team
    )
    await db.commit()

    titles = {r.title for r in await service.listing(db, viewer=approver, viewer_roles=set())}
    assert "Still a draft" not in titles


async def test_a_manager_sees_everything(db, team, requester) -> None:
    boss = await person(db, "boss@hamdaz.com", "manager")
    await service.create(
        db, payload={**FORM, "title": "Not the boss's"}, author=requester, team=team
    )
    await db.commit()

    titles = {
        r.title for r in await service.listing(db, viewer=boss, viewer_roles={"manager"})
    }
    assert "Not the boss's" in titles

# ── who may decide a quote ─────────────────────────────────────────────


async def test_leading_a_team_does_not_let_you_approve_its_quotes(
    db, team, requester
) -> None:
    """Running the work and committing the business to a price are different.

    ``team_lead`` used to be an approver and is not one any more. A lead leads;
    approving a quote is a separate authority, given deliberately through the
    ``approver`` role or by managing the team.
    """
    lead = await person(db, "lead@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=lead, role_keys=["team_lead"])
    request = await service.create(db, payload=dict(FORM), author=requester, team=team)
    await db.commit()

    allowed, reason = await service.may_approve(db, request, user=lead, roles=set())
    assert allowed is False
    assert "approver" in reason


async def test_a_plain_member_may_not_approve(db, team, requester) -> None:
    colleague = await person(db, "colleague-member@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=colleague, role_keys=["member"])
    request = await service.create(db, payload=dict(FORM), author=requester, team=team)
    await db.commit()

    allowed, _ = await service.may_approve(db, request, user=colleague, roles=set())
    assert allowed is False


async def test_an_approver_and_a_team_manager_both_may(db, team, requester) -> None:
    boss = await person(db, "teamboss@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=boss, role_keys=["team_manager"])
    request = await service.create(db, payload=dict(FORM), author=requester, team=team)
    await db.commit()

    allowed, _ = await service.may_approve(db, request, user=boss, roles=set())
    assert allowed is True


async def test_a_team_lead_is_not_emailed_as_an_approver(db, team, requester) -> None:
    """The notification list follows the same rule, so nobody is told to do
    something the system will then refuse them."""
    lead = await person(db, "lead2@hamdaz.com")
    await teams.set_member_roles(db, team=team, user=lead, role_keys=["team_lead"])
    await db.commit()

    people = await service.approvers_for(db, team.id)
    assert lead.id not in {p.id for p in people}

