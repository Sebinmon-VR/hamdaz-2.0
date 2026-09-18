"""The quote request HTTP surface.

The approval rules are covered in test_quoting.py against the service. What is
tested here is the round trip: a request written and then serialised back inside
the same async session. That last step is where this module has actually broken.

The router writes twice — the form, then the win probability read out of the
estimate history — and anything the second write leaves unloaded is fetched
lazily when the response is built. Inside async code a lazy fetch from a
synchronous serialiser is not a slow response, it is a ``MissingGreenlet`` and a
500. So these tests assert on a *serialised body*, not on a status code alone:
the fields that get left behind are timestamps and relationship names, and a
test that only checks for 201 would not notice either.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.access import service as access
from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.comparison.extraction import ExtractedItem, ExtractedQuote, QuoteExtractor
from app.core.config import get_settings
from app.core.mail import MailError
from app.core.security import sign
from app.models.proposal_index import ProposalIndexItem
from app.models.quoting import QuoteRequest, QuoteStatus
from app.quoting.mailer import QuoteMailer
from app.quoting.probability import WinEstimate
from app.roles import service as roles_service
from app.teams import service as teams

SESSION_COOKIE = "hamdaz_session"
API = "/api/v1/quote-requests"

#: As a supplier actually writes it — longer than any column should cap.
LONG_DELIVERY = (
    "2 Weeks - In Stock (Subject to stock availability at time of order and to "
    "export licence restrictions from Huawei side, confirmed at PO)"
)
ZIP_MAGIC = b"PK\x03\x04"
ALPHA_CSV = b"Item,Qty,Price\nFG-201G,2,12000\n"
BETA_CSV = b"Item,Qty,Price\nFG-201G,2,11000\n"

FIRST_TASK_TITLE = "MR-TRJ-24-01-0790 Firewall refresh"

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


class StubWinRates:
    """Stands in for the estimate history.

    The smoothing itself is arithmetic and belongs in its own test; what matters
    to the router is that a probability comes back and is written onto the
    record before the response is built.
    """

    async def estimate(self, zoho, *, customer_name: str | None, refresh: bool = False):
        return WinEstimate(Decimal("0.42"), {"source": "stub", "customer": customer_name})


def _extracted(supplier: str, unit_price: float) -> ExtractedQuote:
    return ExtractedQuote(
        supplier_name=supplier,
        quote_number="Q-1",
        quote_date="2026-09-01",
        currency="AED",
        validity="30 days",
        delivery_time=LONG_DELIVERY,
        payment_terms="30 days",
        warranty="1 year",
        incoterms="DDP",
        contact="sales@example.com",
        discount=0,
        freight=0,
        tax=0,
        quoted_total=unit_price * 2,
        items=[
            ExtractedItem(
                description="FortiGate 201G",
                part_number="FG-201G",
                brand="Fortinet",
                unit="each",
                quantity=2,
                unit_price=unit_price,
                line_total=unit_price * 2,
                lead_time="",
            )
        ],
        note="",
    )


class StubExtractor(QuoteExtractor):
    """Reads documents without a model.

    Subclassed with the key blanked rather than mocked, so the matching that
    runs is the part-number fallback an engineer with no Anthropic credit
    actually gets.
    """

    def __init__(self) -> None:
        settings = get_settings().model_copy()
        settings.anthropic_api_key = ""
        super().__init__(settings)
        self.results: list = []

    async def read_all(self, readables):
        return list(self.results)


#: Where the stub site puts the caller. A seeded row has to carry this to be
#: theirs — it is the column SharePoint's own filter uses. See StubSharePoint.
LOOKUP_ID = "15"


async def seed_task(db, **over) -> ProposalIndexItem:
    """One row of the mirrored Proposals list, as a sync would have left it.

    Every field, because the quoting page shows the whole enquiry rather than a
    summary of it — a task with half its columns dropped on the way through is
    the thing this endpoint exists to avoid.

    **Dates are relative to today on purpose.** Whether a row is live is derived
    from its closing date when it is read, not taken from the stored column, so
    a fixed date would make these tests pass until that date and then quietly
    stop testing anything. Pass ``closing`` to move the bid.

    ``is_open`` and ``is_active`` are set here only because the columns are not
    nullable. Nothing under test reads them; they are derived from the status
    and the closing date. See ``mirror.to_task``.
    """
    closing = over.pop("closing", date.today() + timedelta(days=12))
    row = ProposalIndexItem(
        item_id="412",
        title=FIRST_TASK_TITLE,
        status="In Progress",
        effective_status="In Progress",
        priority="High",
        assigned_lookup_id=LOOKUP_ID,
        assigned_name="Engineer",
        assigned_email="engineer@hamdaz.com",
        start_date=date.today() - timedelta(days=17),
        due_date=closing,
        bid_closing_date=closing,
        deadline=closing,
        end_user="ADNOC Onshore",
        submission_status="Not submitted",
        current_type="Tender",
        order_status=None,
        negotiation=None,
        quote_no="QT-00218",
        remarks="Budgetary only at this stage.",
        working_notes="Waiting on Fortinet pricing.",
        sp_created_at=datetime.now(UTC) - timedelta(days=29),
        sp_modified_at=datetime.now(UTC) - timedelta(days=16),
        has_attachments=False,
        is_open=True,
        is_active=True,
        search_text="",
    )
    for key, value in over.items():
        setattr(row, key, value)
    db.add(row)
    # Committed rather than flushed: the request runs on its own session.
    await db.commit()
    return row


class StubSharePoint:
    """The caller's tasks, without the SharePoint site.

    Only what this router asks for. Whose tasks these are is settled by the
    session in the real client and here too: there is no argument that changes
    it.
    """

    async def lookup_id_for(self, email: str) -> str | None:
        return LOOKUP_ID

    async def tasks_assigned_to(self, lookup_id: str, limit: int = 200):
        # The quoting screen reads the mirror now, and must keep doing so: this
        # client pages and truncates, and the ceiling fell on the live rows.
        raise AssertionError("the quoting screen reads the mirror, not the list")


class StubMailer(QuoteMailer):
    """Everything except the network.

    Subclassed rather than mocked so the real subject and body are built: the
    formatting is the part that breaks, and a mock would prove only that a
    method was called.
    """

    def __init__(self) -> None:
        super().__init__(get_settings(), None)
        self.sent: list[dict] = []
        self.fail: Exception | None = None

    async def send(self, *, sender, recipients, subject, html):
        if self.fail is not None:
            raise self.fail
        self.sent.append(
            {"sender": sender, "recipients": recipients, "subject": subject, "html": html}
        )
        return {"sent": True, "recipients": recipients}


@pytest.fixture
def quoting(client):
    """Zoho stubbed out. Nothing in this module writes to it, and no test reads it."""
    client._transport.app.state.win_rates = StubWinRates()
    client._transport.app.state.zoho = object()
    client._transport.app.state.quote_extractor = StubExtractor()
    client._transport.app.state.sharepoint = StubSharePoint()
    client._transport.app.state.quote_mailer = StubMailer()
    return client


@pytest.fixture
async def approver(db, team):
    user = await upsert_user(
        db,
        EntraIdentity(
            object_id="approver@hamdaz.com",
            email="approver@hamdaz.com",
            display_name="Approver",
        ),
    )
    await teams.set_member_roles(db, team=team, user=user, role_keys=["approver"])
    await db.commit()
    return user


async def _priced(quoting, requester, team, *, markup=15):
    """A quote uploaded, compared, priced from the cheaper supplier."""
    quoting._transport.app.state.quote_extractor.results = [
        _extracted("Alpha Trading", 12000),
        _extracted("Beta Supplies", 11000),
    ]
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()
    attached = (
        await quoting.post(
            f"{API}/{created['id']}/supplier-quotes",
            files=[
                ("files", ("alpha.csv", io.BytesIO(ALPHA_CSV), "text/csv")),
                ("files", ("beta.csv", io.BytesIO(BETA_CSV), "text/csv")),
            ],
        )
    ).json()
    beta = next(
        s for s in attached["comparison"]["suppliers"] if s["supplier_name"] == "Beta Supplies"
    )
    await quoting.post(
        f"{API}/{created['id']}/select-supplier",
        json={"supplier_quote_id": beta["quote_id"], "markup_percent": markup},
    )
    return created["id"]


def _as(client, user):
    client.cookies.set(
        SESSION_COOKIE,
        sign(
            {"sub": str(user.id)},
            secret=get_settings().session_secret,
            ttl_minutes=60,
            audience=SESSION_AUDIENCE,
        ),
    )
    return client


@pytest.fixture
async def team(db):
    """A team that holds the module.

    Granted rather than assumed: these routes carry customer prices and the cost
    behind them, and the tests attack that from the outside rather than trusting
    the dependency is wired up.
    """
    await roles_service.seed_system_roles(db)
    await access.seed_modules(db)
    t = await teams.create_team(db, name="Presales")
    await access.grant_module(db, team=t, module_key="quote_requests")
    await db.commit()
    return t


@pytest.fixture
async def outsider(db, team):
    """Signed in, on no team that holds the module."""
    user = await upsert_user(
        db,
        EntraIdentity(
            object_id="nobody@hamdaz.com",
            email="nobody@hamdaz.com",
            display_name="Nobody",
        ),
    )
    await db.commit()
    return user


@pytest.fixture
async def requester(db, team):
    user = await upsert_user(
        db,
        EntraIdentity(
            object_id="engineer@hamdaz.com",
            email="engineer@hamdaz.com",
            display_name="Engineer",
        ),
    )
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await db.commit()
    return user


async def test_a_member_sees_only_their_own_quotes(
    quoting, db, requester, approver, team
) -> None:
    """A quote is somebody's negotiation with a customer, not a noticeboard.
    The people who decide it see it; a colleague on the same team does not."""
    raised = await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)
    assert raised.status_code == 201, raised.text
    quote_id = raised.json()["id"]

    colleague = await upsert_user(
        db,
        EntraIdentity(
            object_id="colleague@hamdaz.com",
            email="colleague@hamdaz.com",
            display_name="Colleague",
        ),
    )
    await teams.set_member_roles(db, team=team, user=colleague, role_keys=["member"])
    await db.commit()

    def ids(response):
        assert response.status_code == 200, response.text
        return [q["id"] for q in response.json()]

    assert quote_id in ids(await _as(quoting, requester).get(API))
    assert quote_id in ids(await _as(quoting, approver).get(API))
    assert quote_id not in ids(await _as(quoting, colleague).get(API))


async def test_a_stranger_cannot_raise_a_quote(quoting, team) -> None:
    response = await quoting.post(f"{API}?team={team.id}", json=FORM)
    assert response.status_code == 401


async def test_a_user_without_the_module_is_refused(quoting, db, outsider, team) -> None:
    """Customer prices and the margin behind them are not company-wide reading."""
    response = await _as(quoting, outsider).get(API)

    assert response.status_code == 403
    assert "Quote Requests" in response.json()["detail"]


async def test_the_gate_covers_every_route(quoting, db, outsider, team) -> None:
    """Including the ones added after it, which is why it is on the router."""
    signed_in = _as(quoting, outsider)

    assert (await signed_in.get(f"{API}/tasks")).status_code == 403
    assert (await signed_in.get(f"{API}/queue")).status_code == 403
    assert (await signed_in.post(f"{API}?team={team.id}", json=FORM)).status_code == 403


async def test_raising_a_quote_returns_the_whole_saved_request(
    quoting, db, requester, team
) -> None:
    """The regression: a body, fully serialised, after the second write."""
    response = await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)

    assert response.status_code == 201, response.text
    body = response.json()
    # Written by the second flush, and read back in the same breath.
    assert Decimal(body["win_probability"]) == Decimal("0.42")
    assert body["win_basis"]["source"] == "stub"
    # Server-generated, and expired by that second write unless it is not.
    assert body["created_at"] and body["updated_at"]
    # Relationships, which a freshly built row does not have loaded for free.
    assert body["team_name"] == "Presales"
    assert body["created_by_name"] == "Engineer"
    assert body["assigned_to_name"] == "Engineer"
    assert body["status"] == "draft"
    assert [i["name"] for i in body["items"]] == ["FortiGate 201G", "3yr subscription"]
    assert body["may_edit"] is True
    # Nobody approves their own quote, however senior.
    assert body["may_approve"] is False


async def test_editing_a_quote_returns_it_with_a_moved_timestamp(
    quoting, db, requester, team
) -> None:
    """The same trap on the update path, where ``updated_at`` is recomputed."""
    created = (
        await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)
    ).json()

    response = await quoting.patch(
        f"{API}/{created['id']}", json={**FORM, "title": "Firewall refresh, rev B"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Firewall refresh, rev B"
    assert body["updated_at"] != created["updated_at"]
    assert body["team_name"] == "Presales"


async def test_a_quote_for_a_team_that_does_not_exist_is_refused(
    quoting, db, requester
) -> None:
    response = await _as(quoting, requester).post(f"{API}?team=no-such-team", json=FORM)
    assert response.status_code == 404


# ── the supplier quotes behind it ──────────────────────────────────────


async def test_attaching_supplier_quotes_returns_the_comparison(
    quoting, db, requester, team
) -> None:
    """The second trap on this route: ``comparison`` is a row, the body wants
    what it concluded."""
    quoting._transport.app.state.quote_extractor.results = [
        _extracted("Alpha Trading", 12000),
        _extracted("Beta Supplies", 11000),
    ]
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()

    response = await quoting.post(
        f"{API}/{created['id']}/supplier-quotes",
        files=[
            ("files", ("alpha.csv", io.BytesIO(ALPHA_CSV), "text/csv")),
            ("files", ("beta.csv", io.BytesIO(BETA_CSV), "text/csv")),
        ],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["multiple_supplier_quotes"] is True
    assert body["comparison_id"]
    # The analysis, not the comparison record it is stored on.
    assert body["comparison"]["supplier_count"] == 2
    assert body["comparison"]["cheapest_supplier"]["supplier_name"] == "Beta Supplies"
    # Stored whole, conditions and all.
    assert LONG_DELIVERY in str(body["comparison"])
    # And the rest of the quote still comes back whole.
    assert body["team_name"] == "Presales"
    assert body["title"] == "Firewall refresh"


async def test_a_quote_with_no_readable_supplier_quote_is_refused(
    quoting, db, requester, team
) -> None:
    """A file nothing can read is a message, not a 500."""
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()

    response = await quoting.post(
        f"{API}/{created['id']}/supplier-quotes",
        files=[("files", ("quote.zip", io.BytesIO(ZIP_MAGIC), "application/zip"))],
    )

    assert response.status_code == 400
    assert "quote.zip" in response.json()["detail"]


# ── pricing it from one of them ────────────────────────────────────────


async def test_choosing_a_supplier_prices_the_quote_and_opens_approval(
    quoting, db, requester, team
) -> None:
    """The whole round trip: uploaded, compared, chosen, priced, submittable."""
    quoting._transport.app.state.quote_extractor.results = [
        _extracted("Alpha Trading", 12000),
        _extracted("Beta Supplies", 11000),
    ]
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()
    attached = (
        await quoting.post(
            f"{API}/{created['id']}/supplier-quotes",
            files=[
                ("files", ("alpha.csv", io.BytesIO(ALPHA_CSV), "text/csv")),
                ("files", ("beta.csv", io.BytesIO(BETA_CSV), "text/csv")),
            ],
        )
    ).json()

    # Attached but unchosen: nothing to approve yet, and the form is told why
    # rather than finding out when the button is pressed.
    assert attached["may_submit"] is False
    assert "none has been chosen" in attached["submit_reason"]

    beta = next(
        s for s in attached["comparison"]["suppliers"] if s["supplier_name"] == "Beta Supplies"
    )
    response = await quoting.post(
        f"{API}/{created['id']}/select-supplier",
        json={"supplier_quote_id": beta["quote_id"], "markup_percent": 15},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [i["name"] for i in body["items"]] == ["FortiGate 201G"]
    # Their price is our cost, and the rate is that plus the margin.
    assert Decimal(body["items"][0]["cost_rate"]) == Decimal(11000)
    assert Decimal(body["items"][0]["rate"]) == Decimal(12650)
    assert Decimal(body["total"]) == Decimal(12650) * 2
    assert body["selected_supplier_quote_id"] == beta["quote_id"]
    assert body["may_submit"] is True
    assert body["submit_reason"] is None

    sent = await quoting.post(f"{API}/{created['id']}/submit")

    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "pending_approval"


async def test_the_lines_stay_editable_after_a_supplier_is_chosen(
    quoting, db, requester, team
) -> None:
    """Priced from a supplier, then customised — added lines and all."""
    quoting._transport.app.state.quote_extractor.results = [_extracted("Alpha Trading", 12000)]
    created = (await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)).json()
    attached = (
        await quoting.post(
            f"{API}/{created['id']}/supplier-quotes",
            files=[("files", ("alpha.csv", io.BytesIO(ALPHA_CSV), "text/csv"))],
        )
    ).json()
    await quoting.post(
        f"{API}/{created['id']}/select-supplier",
        json={"supplier_quote_id": attached["comparison"]["suppliers"][0]["quote_id"]},
    )

    response = await quoting.patch(
        f"{API}/{created['id']}",
        json={
            **FORM,
            "items": [
                {"name": "FortiGate 201G", "quantity": 2, "rate": 15000, "cost_rate": 12000},
                {"name": "Installation", "quantity": 1, "rate": 2500},
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [i["name"] for i in body["items"]] == ["FortiGate 201G", "Installation"]
    assert Decimal(body["items"][0]["margin"]) == Decimal(6000)
    # Still priced from that supplier, and still submittable.
    assert body["selected_supplier_quote_id"] == attached["comparison"]["suppliers"][0]["quote_id"]
    assert body["may_submit"] is True


async def test_the_currency_is_correctable_after_the_quote_has_gone_up(
    quoting, db, requester, approver, team
) -> None:
    """Everything else on a submitted quote freezes, and should. The currency
    does not.

    Nothing in this module converts between currencies, so the code is a label
    on figures that are already what they are — changing it restates no number
    and invalidates no approval. Freezing it too would mean pulling a quote back
    out of approval, and re-notifying every approver, to correct three letters.
    """
    raised = await _as(quoting, requester).post(f"{API}?team={team.id}", json=FORM)
    assert raised.status_code == 201, raised.text
    quote_id = raised.json()["id"]

    # Put straight into the frozen state rather than submitted through the
    # workflow: what is under test here is the rule, not the road to it.
    quote = await db.get(QuoteRequest, uuid.UUID(quote_id))
    quote.status = QuoteStatus.PENDING_APPROVAL
    await db.commit()

    client = _as(quoting, requester)

    # The ordinary edit is refused, exactly as it was before.
    frozen = await client.patch(f"{API}/{quote_id}", json=FORM)
    assert frozen.status_code == 403
    assert "cannot be edited" in frozen.json()["detail"]

    # The currency still moves. Codes are codes, so it comes back upper-cased.
    changed = await client.patch(f"{API}/{quote_id}/currency", json={"currency": "usd"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["currency"] == "USD"
    assert changed.json()["status"] == "pending_approval"
    assert changed.json()["may_set_currency"] is True

    # Whose quote it is still decides. An approver may decide this quote but
    # does not own it, so they ask the person who raised it.
    denied = await _as(quoting, approver).patch(
        f"{API}/{quote_id}/currency", json={"currency": "EUR"}
    )
    assert denied.status_code == 403
    assert "not yours" in denied.json()["detail"]

    # A super admin is the exception, and holds it in any state. They should not
    # have to message the author to correct a three-letter label.
    boss = await upsert_user(
        db,
        EntraIdentity(
            object_id="boss@hamdaz.com",
            email="boss@hamdaz.com",
            display_name="Boss",
        ),
    )
    await roles_service.assign_role(
        db, user_id=boss.id, role_key="super_admin", granted_by_id=None
    )
    await db.commit()
    theirs = await _as(quoting, boss).patch(
        f"{API}/{quote_id}/currency", json={"currency": "GBP"}
    )
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["currency"] == "GBP"


# ── starting from a task ───────────────────────────────────────────────


async def test_a_quote_can_be_raised_from_one_of_your_tasks(
    quoting, db, requester, team
) -> None:
    """The enquiry is already written down; retyping it is where errors come from."""
    task = await seed_task(db)

    response = await _as(quoting, requester).post(
        f"{API}/from-task?team={team.id}", json={"task_id": "412"}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["title"] == "MR-TRJ-24-01-0790 Firewall refresh"
    assert body["customer_name"] == "ADNOC Onshore"
    assert body["reference"] == "QT-00218"
    assert body["cf_bcd"].startswith(task.bid_closing_date.isoformat())
    assert "Budgetary only" in body["notes"]
    # The enquiry and the quote stay connected.
    assert body["source_task_id"] == "412"
    assert body["source_task_url"].endswith("ID=412")
    # A probability from the moment it was raised, with its basis.
    assert body["win_basis"]["source"] == "stub"
    # Nothing to send yet: a task has no prices on it, which is the point of
    # raising the quote.
    assert body["may_submit"] is False
    assert "at least one line" in body["submit_reason"]


async def test_a_task_that_is_not_yours_is_not_yours_to_quote(
    quoting, db, requester, team
) -> None:
    """Whose tasks these are comes from the session, not from the request."""
    await seed_task(db, item_id="999")

    response = await _as(quoting, requester).post(
        f"{API}/from-task?team={team.id}", json={"task_id": "412"}
    )

    assert response.status_code == 404
    assert "not one of yours" in response.json()["detail"]


# ── negotiation ────────────────────────────────────────────────────────


async def test_a_negotiation_reopens_the_quote_with_the_approved_round_attached(
    quoting, db, requester, approver, team
) -> None:
    """What the next reviewer needs: the numbers that were agreed, still readable."""
    quote_id = await _priced(quoting, requester, team)
    await quoting.post(f"{API}/{quote_id}/submit")
    approved = (
        await _as(quoting, approver).post(
            f"{API}/{quote_id}/reviews", json={"action": "approve"}
        )
    ).json()
    assert approved["status"] == "approved"

    response = await _as(quoting, requester).post(
        f"{API}/{quote_id}/negotiate",
        json={"note": "Customer wants 8% off and delivery in three weeks."},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_negotiation"
    # Theirs to reprice again.
    assert body["may_edit"] is True
    assert body["revision"] == 2
    # The round that was approved, whole.
    assert len(body["revisions"]) == 1
    kept = body["revisions"][0]
    assert kept["outcome"] == "approve"
    assert Decimal(kept["snapshot"]["items"][0]["rate"]) == Decimal(12650)
    assert kept["snapshot"]["win_probability"] is not None
    # And what the customer asked for, in the history beside it.
    assert body["reviews"][-1]["action"] == "negotiate"
    assert "8% off" in body["reviews"][-1]["note"]


async def test_a_quote_still_being_approved_cannot_be_negotiated(
    quoting, db, requester, team
) -> None:
    quote_id = await _priced(quoting, requester, team)
    await quoting.post(f"{API}/{quote_id}/submit")

    response = await quoting.post(
        f"{API}/{quote_id}/negotiate", json={"note": "They want a discount."}
    )

    assert response.status_code == 400
    assert "approved quote" in response.json()["detail"]


# ── the tasks to choose from ───────────────────────────────────────────


async def test_the_quoting_page_lists_the_callers_own_tasks(
    quoting, db, requester, team
) -> None:
    """The enquiry list is where a quote starts, so it is served here."""
    await seed_task(db)
    await seed_task(
        db,
        item_id="500",
        title="Switch refresh",
        closing=date.today() + timedelta(days=3),
    )

    response = await _as(quoting, requester).get(f"{API}/tasks")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["in_sharepoint"] is True
    assert body["total"] == 2
    assert body["live_count"] == 2
    assert body["quoted_count"] == 0
    # Soonest deadline first.
    assert [t["title"] for t in body["tasks"]] == ["Switch refresh", FIRST_TASK_TITLE]
    # The whole row, not a summary of it.
    task = body["tasks"][1]
    assert task["end_user"] == "ADNOC Onshore"
    assert task["priority"] == "High"
    assert task["current_type"] == "Tender"
    assert task["submission_status"] == "Not submitted"
    assert task["quote_no"] == "QT-00218"
    assert task["working_notes"] == "Waiting on Fortinet pricing."
    assert task["web_url"].endswith("ID=412")
    # Nothing raised against it yet.
    assert task["quote_request_id"] is None


async def test_the_task_list_shows_live_work_first_and_the_rest_on_request(
    quoting, db, requester, team
) -> None:
    """Most of what is assigned to anybody is a bid that closed months ago and
    was never marked finished. Listing those first buried the handful that can
    still be quoted for."""
    await seed_task(db)
    # Not finished, but its bid closed: open, and not live.
    await seed_task(
        db,
        item_id="300",
        title="Closed bid",
        closing=date.today() - timedelta(days=60),
        is_active=False,
    )
    # Finished, so neither.
    await seed_task(
        db,
        item_id="200",
        title="Finished",
        status="Completed",
        effective_status="Completed",
        closing=date.today() - timedelta(days=90),
        is_open=False,
        is_active=False,
    )
    client = _as(quoting, requester)

    live = (await client.get(f"{API}/tasks")).json()
    assert [t["id"] for t in live["tasks"]] == ["412"]
    assert (live["live_count"], live["open_count"], live["total"]) == (1, 2, 3)

    opened = (await client.get(f"{API}/tasks?scope=open")).json()
    assert sorted(t["id"] for t in opened["tasks"]) == ["300", "412"]

    everything = (await client.get(f"{API}/tasks?scope=all")).json()
    assert sorted(t["id"] for t in everything["tasks"]) == ["200", "300", "412"]


async def test_a_task_that_already_has_a_quote_says_so(
    quoting, db, requester, team
) -> None:
    """Otherwise the list offers to raise a second quote for the same enquiry."""
    await seed_task(db)
    raised = (
        await _as(quoting, requester).post(
            f"{API}/from-task?team={team.id}", json={"task_id": "412"}
        )
    ).json()

    body = (await quoting.get(f"{API}/tasks")).json()

    assert body["quoted_count"] == 1
    task = body["tasks"][0]
    assert task["quote_request_id"] == raised["id"]
    assert task["quote_status"] == "draft"
    assert task["quote_title"] == FIRST_TASK_TITLE


async def test_somebody_with_no_sharepoint_account_is_told_so(
    quoting, db, requester, team
) -> None:
    """Having no tasks and being invisible to SharePoint are different things."""

    async def nobody(email):
        return None

    quoting._transport.app.state.sharepoint.lookup_id_for = nobody

    body = (await _as(quoting, requester).get(f"{API}/tasks")).json()

    assert body["in_sharepoint"] is False
    assert body["tasks"] == []


async def test_a_stranger_gets_no_task_list(quoting) -> None:
    assert (await quoting.get(f"{API}/tasks")).status_code == 401


# ── telling the approvers ──────────────────────────────────────────────


async def test_submitting_mails_the_approvers_a_link_to_the_quote(
    quoting, db, requester, approver, team
) -> None:
    """An approval nobody is told about waits until somebody happens to look."""
    quote_id = await _priced(quoting, requester, team)
    mailer = quoting._transport.app.state.quote_mailer

    response = await quoting.post(f"{API}/{quote_id}/submit")

    assert response.status_code == 200, response.text
    assert len(mailer.sent) == 1
    mail = mailer.sent[0]
    assert mail["recipients"] == ["approver@hamdaz.com"]
    # From the requester's own mailbox, so a reply reaches them.
    assert mail["sender"] == "engineer@hamdaz.com"
    assert "ADNOC" in mail["subject"]
    # The link is the point of the message.
    assert f"/quote-requests/{quote_id}" in mail["html"]
    assert "AED 25,300.00" in mail["html"]
    # And the quote records that it went.
    body = response.json()
    assert body["approvers_notified_at"] is not None
    assert body["notify_error"] is None


async def test_nobody_is_mailed_about_the_thing_they_just_did(
    quoting, db, requester, team
) -> None:
    """The requester approves for this team too, and still should not get mail."""
    await teams.set_member_roles(db, team=team, user=requester, role_keys=["approver"])
    await db.commit()
    quote_id = await _priced(quoting, requester, team)
    mailer = quoting._transport.app.state.quote_mailer

    await quoting.post(f"{API}/{quote_id}/submit")

    assert all("engineer@hamdaz.com" not in m["recipients"] for m in mailer.sent)


async def test_a_quote_still_goes_up_when_the_mail_does_not(
    quoting, db, requester, approver, team
) -> None:
    """The email is a notification, not the transaction — but the failure shows."""
    quote_id = await _priced(quoting, requester, team)
    quoting._transport.app.state.quote_mailer.fail = MailError("mailbox not found")

    response = await quoting.post(f"{API}/{quote_id}/submit")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending_approval"
    # Nobody was told, and the person who sent it can see that.
    assert body["approvers_notified_at"] is None
    assert "mailbox not found" in body["notify_error"]


async def test_the_requester_hears_what_was_decided(
    quoting, db, requester, approver, team
) -> None:
    """A decision nobody is told about is a quote that looks stuck."""
    quote_id = await _priced(quoting, requester, team)
    await quoting.post(f"{API}/{quote_id}/submit")
    mailer = quoting._transport.app.state.quote_mailer
    mailer.sent.clear()

    await _as(quoting, approver).post(
        f"{API}/{quote_id}/reviews",
        json={"action": "rework", "note": "Margin on line 1 is too thin."},
    )

    assert len(mailer.sent) == 1
    mail = mailer.sent[0]
    assert mail["recipients"] == ["engineer@hamdaz.com"]
    # From the approver, so it can be argued with.
    assert mail["sender"] == "approver@hamdaz.com"
    assert "sent back for changes" in mail["subject"]
    # And why, which is the part that lets them act on it.
    assert "Margin on line 1 is too thin." in mail["html"]
    assert f"/quote-requests/{quote_id}" in mail["html"]


async def test_an_approval_is_told_to_the_person_who_raised_it(
    quoting, db, requester, approver, team
) -> None:
    quote_id = await _priced(quoting, requester, team)
    await quoting.post(f"{API}/{quote_id}/submit")
    mailer = quoting._transport.app.state.quote_mailer
    mailer.sent.clear()

    await _as(quoting, approver).post(f"{API}/{quote_id}/reviews", json={"action": "approve"})

    assert mailer.sent[0]["recipients"] == ["engineer@hamdaz.com"]
    assert "approved" in mailer.sent[0]["subject"]


async def test_an_approvers_comment_reaches_the_people_who_own_the_quote(
    quoting, db, requester, approver, team
) -> None:
    quote_id = await _priced(quoting, requester, team)
    await quoting.post(f"{API}/{quote_id}/submit")
    mailer = quoting._transport.app.state.quote_mailer
    mailer.sent.clear()

    response = await _as(quoting, approver).post(
        f"{API}/{quote_id}/comments",
        json={"target_type": "field", "target_ref": "discount", "body": "Why the 5%?"},
    )

    assert response.status_code == 201, response.text
    assert mailer.sent[0]["recipients"] == ["engineer@hamdaz.com"]
    # Anchored, and the mail says what to.
    assert "on discount" in mailer.sent[0]["html"]
    assert "Why the 5%?" in mailer.sent[0]["html"]


async def test_the_requesters_reply_goes_back_to_the_approvers(
    quoting, db, requester, approver, team
) -> None:
    """A comment reaches the other side of the quote, whichever side that is."""
    quote_id = await _priced(quoting, requester, team)
    await quoting.post(f"{API}/{quote_id}/submit")
    mailer = quoting._transport.app.state.quote_mailer
    mailer.sent.clear()

    await _as(quoting, requester).post(
        f"{API}/{quote_id}/comments",
        json={"target_type": "quote", "body": "Volume discount, agreed on the call."},
    )

    assert mailer.sent[0]["recipients"] == ["approver@hamdaz.com"]
    assert mailer.sent[0]["sender"] == "engineer@hamdaz.com"


async def test_reopening_tells_the_approvers_their_approval_no_longer_stands(
    quoting, db, requester, approver, team
) -> None:
    quote_id = await _priced(quoting, requester, team)
    await quoting.post(f"{API}/{quote_id}/submit")
    await _as(quoting, approver).post(f"{API}/{quote_id}/reviews", json={"action": "approve"})
    mailer = quoting._transport.app.state.quote_mailer
    mailer.sent.clear()

    await _as(quoting, requester).post(
        f"{API}/{quote_id}/negotiate", json={"note": "Customer wants 8% off."}
    )

    assert mailer.sent[0]["recipients"] == ["approver@hamdaz.com"]
    assert "reopened for negotiation" in mailer.sent[0]["subject"]
    assert "Customer wants 8% off." in mailer.sent[0]["html"]


async def test_a_global_approver_is_mailed_too(quoting, db, requester, team) -> None:
    """The bug this had: reading a user off a role grant that has no user on it.

    It only bites when somebody holds an organisation-wide approving role, which
    in a real deployment is everybody senior — and the failure is swallowed,
    because a notification must never fail the quote. So nobody would be mailed
    and nothing would say so.
    """
    boss = await upsert_user(
        db,
        EntraIdentity(
            object_id="ceo@hamdaz.com", email="ceo@hamdaz.com", display_name="Chief"
        ),
    )
    await roles_service.assign_role(db, user_id=boss.id, role_key="ceo", granted_by_id=None)
    await db.commit()
    quote_id = await _priced(quoting, requester, team)
    mailer = quoting._transport.app.state.quote_mailer
    mailer.sent.clear()

    response = await quoting.post(f"{API}/{quote_id}/submit")

    assert response.json()["notify_error"] is None
    assert "ceo@hamdaz.com" in mailer.sent[0]["recipients"]
