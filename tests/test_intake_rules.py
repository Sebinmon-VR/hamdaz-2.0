"""The mail intake's decisions, made without a database, Graph or a model.

The parts worth testing hardest are the ones that say no or say "I am not
sure", because the cost of being wrong here is a real row in the live Proposals
list assigned to a real person, raised from an email that was actually a
thank-you note.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from app.intake.classifier import Classification
from app.intake.graph_mail import BODY_LIMIT, sender_allowed, summarise_message
from app.intake.service import _mark_negotiation, _task_fields
from app.models.intake import (
    CREATING_CATEGORIES,
    IntakeAction,
    IntakeMessage,
    IntakeSettings,
    IntakeStatus,
    MailCategory,
)
from app.models.proposal_index import ProposalIndexItem
from app.notifications.service import _card, send_to_teams
from app.proposals.mirror import (
    Candidate,
    _cosine,
    normalise,
    references_in,
    search_text_of,
)


@pytest.fixture
def db_free_session():
    """Stands in for a session where the code under test only flushes.

    These rules touch no table — they decide what to write and whether to write
    it — so a real database would only make them slow and would not test
    anything the pipeline tests do not already cover.
    """

    class _Session:
        async def flush(self) -> None:
            return None

    return _Session()


# ── who is listened to ─────────────────────────────────────────────────


def test_an_unconfigured_intake_listens_to_nobody() -> None:
    """The opposite of the usual convention, and the point of it: a blank
    configuration must not mean "read everything in this mailbox"."""
    assert sender_allowed("ceo@hamdaz.com", addresses=[], domains=[]) is False


def test_a_named_sender_is_admitted_whatever_the_case() -> None:
    assert sender_allowed(
        "CEO@Hamdaz.COM", addresses=["ceo@hamdaz.com"], domains=[]
    ) is True


def test_a_whole_domain_can_be_admitted() -> None:
    assert sender_allowed("buyer@client.ae", addresses=[], domains=["client.ae"]) is True
    assert sender_allowed("buyer@client.ae", addresses=[], domains=["@client.ae"]) is True


def test_a_stranger_is_refused() -> None:
    assert sender_allowed(
        "spam@elsewhere.com", addresses=["ceo@hamdaz.com"], domains=["client.ae"]
    ) is False


def test_a_message_with_no_sender_is_refused() -> None:
    assert sender_allowed(None, addresses=["ceo@hamdaz.com"], domains=[]) is False


def test_a_lookalike_domain_is_refused() -> None:
    """``notclient.ae`` ends with ``client.ae``. A substring test would admit it."""
    assert sender_allowed("x@notclient.ae", addresses=[], domains=["client.ae"]) is False


def test_a_subdomain_of_an_admitted_domain_is_admitted() -> None:
    """Large customers send from ``mail.`` and ``corp.`` subdomains, and
    listing each one by hand is how a filter goes stale."""
    assert sender_allowed("x@mail.adnoc.ae", addresses=[], domains=["adnoc.ae"]) is True


def test_a_bare_label_matches_nothing() -> None:
    """"adnoc" without its ending is the mistake this filter invites: it sits
    in the settings looking configured and admits nobody. The settings refuse
    to save one — this is the behaviour that makes that refusal worth having."""
    assert sender_allowed("x@adnoc.ae", addresses=[], domains=["adnoc"]) is False


# ── reading a message ──────────────────────────────────────────────────


def test_a_graph_message_is_reduced_to_what_the_row_holds() -> None:
    row = summarise_message(
        {
            "id": "AAMk123",
            "conversationId": "conv1",
            "subject": "  RE: T-2291 tender  ",
            "bodyPreview": "Please quote",
            "receivedDateTime": "2026-09-08T09:15:00Z",
            "from": {"emailAddress": {"address": "CEO@Hamdaz.com", "name": "The CEO"}},
            "hasAttachments": True,
        }
    )
    assert row["graph_message_id"] == "AAMk123"
    assert row["sender_email"] == "ceo@hamdaz.com", "lower-cased for comparing"
    assert row["subject"] == "RE: T-2291 tender"
    assert row["received_at"] is not None and row["received_at"].year == 2026
    assert row["has_attachments"] is True


def test_a_long_body_is_trimmed_before_a_model_ever_sees_it() -> None:
    """A forwarded tender with forty quoted replies underneath is mostly
    somebody else's signature block."""
    row = summarise_message(
        {"id": "x", "body": {"content": "y" * (BODY_LIMIT * 2)}}
    )
    assert len(row["body"]) == BODY_LIMIT


def test_a_message_with_no_id_is_not_recorded() -> None:
    assert summarise_message({"subject": "hello"})["graph_message_id"] == ""


# ── finding the reference numbers ──────────────────────────────────────


def test_references_are_pulled_out_of_the_words() -> None:
    found = references_in("Re: T-2291 and QT/1001 — please reopen the KOC bid")
    assert "T-2291" in found and "QT/1001" in found


def test_ordinary_words_are_not_mistaken_for_references() -> None:
    """A reference needs a digit in it. Otherwise every word in a subject line
    becomes something to search the list for."""
    assert references_in("Please quote for the pumping station") == []


def test_the_same_number_written_three_ways_normalises_to_one() -> None:
    """The list contains all three habits, for the same number."""
    assert normalise("QT-1001") == normalise("qt/1001") == normalise("QT 1001")


def test_references_are_deduplicated() -> None:
    found = references_in("T-2291", "about T-2291 again")
    assert found.count("T-2291") == 1


# ── ordering the shortlist ─────────────────────────────────────────────


def _item(**kw) -> ProposalIndexItem:
    row = ProposalIndexItem(item_id=kw.get("item_id", "1"), title=kw.get("title", "A bid"))
    row.quote_no = kw.get("quote_no")
    row.embedding = kw.get("embedding")
    return row


def test_an_exact_reference_outranks_a_close_wording() -> None:
    """Two rows whose words look alike are common; two rows sharing a tender
    number are the same tender."""
    by_reference = Candidate(item=_item(), matched_reference="T-2291", text_rank=0.0)
    by_words = Candidate(item=_item(item_id="2"), similarity=0.9, text_rank=1.0)
    assert by_reference.score > by_words.score


def test_meaning_leads_the_rest_and_wording_breaks_the_tie() -> None:
    closer = Candidate(item=_item(), similarity=0.9, text_rank=0.1)
    further = Candidate(item=_item(item_id="2"), similarity=0.3, text_rank=0.9)
    assert closer.score > further.score


def test_a_row_with_no_signal_scores_nothing() -> None:
    assert Candidate(item=_item()).score == 0.0


# ── the vector stage ───────────────────────────────────────────────────


def test_cosine_finds_the_nearest_row() -> None:
    scores = _cosine([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]])
    assert scores[0] == pytest.approx(1.0)
    assert scores[1] == pytest.approx(0.0)
    assert scores[2] == pytest.approx(-1.0)


def test_a_row_that_failed_to_embed_scores_nothing_rather_than_raising() -> None:
    """A zero-length vector is a row whose embedding did not happen. Dividing
    by its norm would end the whole match."""
    assert _cosine([1.0, 0.0], [[0.0, 0.0]])[0] == pytest.approx(0.0)


def test_an_empty_candidate_set_is_an_empty_answer() -> None:
    assert _cosine([1.0, 0.0], []) == []


# ── what a row is matched on ───────────────────────────────────────────


class _Task:
    def __init__(self, **kw) -> None:
        for field in (
            "title", "end_user", "quote_no", "current_type",
            "submission_status", "remarks", "working_notes",
        ):
            setattr(self, field, kw.get(field))


def test_the_search_text_carries_the_title_and_the_notes() -> None:
    """The customer's own reference is often written only in the notes."""
    text = search_text_of(
        _Task(title="Pump bid", end_user="KOC", working_notes="their ref ABC-9")
    )
    assert "Pump bid" in text and "KOC" in text and "ABC-9" in text


def test_empty_fields_do_not_become_blank_lines() -> None:
    assert search_text_of(_Task(title="Only a title")) == "Only a title"


# ── what would be posted ───────────────────────────────────────────────


def test_the_payload_is_exactly_what_sharepoint_would_receive() -> None:
    """With writing switched off this *is* the output, so it has to be the
    real thing rather than a description of one."""
    fields = _task_fields(
        Classification(
            category=MailCategory.TENDER,
            title="KOC pump bid",
            customer="Kuwait Oil",
            deadline="2026-09-30",
            references=["T-2291"],
            summary="Bid for three pumps",
        ),
        assignee_lookup_id="77",
    )
    assert fields["Title"] == "KOC pump bid"
    assert fields["EndUser"] == "Kuwait Oil"
    assert fields["BCD"] == "2026-09-30"
    assert fields["AssignedToLookupId"] == "77"
    assert "T-2291" in fields["Remarks"]
    assert fields["Status"] == "Not Started"


def test_a_task_with_nobody_to_give_it_to_carries_no_assignee() -> None:
    fields = _task_fields(Classification(title="A bid"), assignee_lookup_id=None)
    assert "AssignedToLookupId" not in fields


def test_an_untitled_message_still_produces_a_usable_title() -> None:
    assert _task_fields(Classification(), assignee_lookup_id=None)["Title"] == "(from email)"


# ── marking a negotiation on the task ──────────────────────────────────
#
# A negotiation never creates a task, so nothing changes in SharePoint on its
# own and a flow watching the list has nothing to trigger on. Setting the
# matched task's Negotiation column gives it one — and that is a write to a
# live list, so when it happens and when it does not are worth pinning.


class _FakeSharePoint:
    def __init__(self) -> None:
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    async def update_task(self, item_id: str, fields: dict[str, Any]):
        if self.fail:
            raise RuntimeError("SharePoint refused")
        self.updated.append((item_id, fields))
        return None


def _settings(**kw) -> IntakeSettings:
    row = IntakeSettings(id=1)
    row.update_negotiation = kw.get("update_negotiation", False)
    row.negotiation_value = kw.get("negotiation_value", "Yes")
    return row


def _row() -> IntakeMessage:
    return IntakeMessage(graph_message_id="m1", status=IntakeStatus.CLASSIFIED)


async def test_the_switch_is_off_so_nothing_is_written(db_free_session) -> None:
    """The payload is recorded and the list is untouched — the same rule as
    creating a task."""
    sharepoint = _FakeSharePoint()
    row, item = _row(), _item(item_id="42")
    action = await _mark_negotiation(
        db_free_session, row, item, intake=_settings(), sharepoint=sharepoint
    )
    assert sharepoint.updated == [], "nothing reached SharePoint"
    assert row.would_update == {"item_id": "42", "fields": {"Negotiation": "Yes"}}
    assert row.status == IntakeStatus.SIMULATED
    assert action == IntakeAction.NEGOTIATION_NOTICE


async def test_with_the_switch_on_the_column_is_set(db_free_session) -> None:
    sharepoint = _FakeSharePoint()
    row, item = _row(), _item(item_id="42")
    action = await _mark_negotiation(
        db_free_session, row, item,
        intake=_settings(update_negotiation=True), sharepoint=sharepoint,
    )
    assert sharepoint.updated == [("42", {"Negotiation": "Yes"})]
    assert action == IntakeAction.MARKED_NEGOTIATION
    assert item.negotiation == "Yes", "kept locally so the next mail in the thread sees it"


async def test_a_task_already_marked_is_not_written_again(db_free_session) -> None:
    """A negotiation is a thread and this runs per message. Rewriting the same
    value would re-fire the flow on every reply."""
    sharepoint = _FakeSharePoint()
    row, item = _row(), _item(item_id="42")
    item.negotiation = "yes"  # whatever case the list happens to hold
    action = await _mark_negotiation(
        db_free_session, row, item,
        intake=_settings(update_negotiation=True), sharepoint=sharepoint,
    )
    assert sharepoint.updated == []
    assert action == IntakeAction.NEGOTIATION_NOTICE
    assert "already" in (row.match_reason or "").lower()


async def test_a_refused_write_still_leaves_the_notice(db_free_session) -> None:
    """Failing to mark the column must not lose the notification with it."""
    sharepoint = _FakeSharePoint()
    sharepoint.fail = True
    row, item = _row(), _item(item_id="42")
    action = await _mark_negotiation(
        db_free_session, row, item,
        intake=_settings(update_negotiation=True), sharepoint=sharepoint,
    )
    assert action == IntakeAction.NEGOTIATION_NOTICE
    assert "Could not set Negotiation" in (row.error or "")


async def test_the_value_written_is_configurable(db_free_session) -> None:
    """The column's choices are the list's business and can change."""
    sharepoint = _FakeSharePoint()
    await _mark_negotiation(
        db_free_session, _row(), _item(item_id="42"),
        intake=_settings(update_negotiation=True, negotiation_value="In negotiation"),
        sharepoint=sharepoint,
    )
    assert sharepoint.updated == [("42", {"Negotiation": "In negotiation"})]


# ── which categories may raise work ────────────────────────────────────


@pytest.mark.parametrize("category", ["tender", "proposal"])
def test_only_new_work_creates_a_task(category: str) -> None:
    assert Classification(category=category).creates_work is True
    assert category in CREATING_CATEGORIES


@pytest.mark.parametrize("category", ["negotiation", "order", "general", "unknown"])
def test_everything_else_never_creates_one(category: str) -> None:
    """A negotiation concerns work that already exists and somebody holds.
    Raising a task for it would duplicate the thing being negotiated."""
    assert Classification(category=category).creates_work is False
    assert category not in CREATING_CATEGORIES


def test_the_matcher_searches_on_the_extraction_not_the_whole_email() -> None:
    """Signature blocks and quoted history are noise to a search."""
    text = Classification(
        title="Pump bid", customer="KOC", summary="Three pumps by September"
    ).match_text
    assert "Pump bid" in text and "KOC" in text


# ── the Teams card ─────────────────────────────────────────────────────


def test_the_card_carries_the_facts_as_a_table() -> None:
    """The facts are what people scan. Formatted into the body text they
    cannot be."""
    card = _card(
        title="New tender",
        body="KOC pumps",
        facts={"Customer": "KOC", "Deadline": "2026-09-30", "Empty": None},
        link="https://app/t/1",
    )
    content = card["attachments"][0]["content"]
    facts = next(b for b in content["body"] if b["type"] == "FactSet")["facts"]
    assert {"title": "Customer", "value": "KOC"} in facts
    assert not any(f["title"] == "Empty" for f in facts), "blank facts are dropped"
    assert content["actions"][0]["url"] == "https://app/t/1"


def test_a_card_with_no_link_has_no_button() -> None:
    card = _card(title="Something", body=None, facts={}, link=None)
    assert "actions" not in card["attachments"][0]["content"]


# ── posting it ─────────────────────────────────────────────────────────
#
# The send is stubbed at the transport, so what is exercised is everything this
# module actually owns: that the card goes to the configured URL, and that a
# refusal or an outage costs a copy rather than the thing that caused it.


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_the_card_is_posted_to_the_configured_url() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text="1")

    async with _client(handler) as http:
        sent = await send_to_teams(
            http,
            "https://outlook.office.com/webhook/abc",
            title="New tender",
            body="KOC pumps",
            facts={"Customer": "KOC"},
            link="https://app/t/1",
        )

    assert sent is True
    assert seen["url"] == "https://outlook.office.com/webhook/abc"
    card = seen["body"]["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["body"][0]["text"] == "New tender"


async def test_a_refused_card_is_reported_not_raised() -> None:
    """The notification is already recorded before this runs, so a webhook that
    has expired costs a copy and not the fact."""
    async with _client(lambda r: httpx.Response(400, text="expired")) as http:
        assert await send_to_teams(http, "https://x/y", title="Anything") is False


async def test_an_unreachable_channel_is_reported_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with _client(handler) as http:
        assert await send_to_teams(http, "https://x/y", title="Anything") is False


async def test_no_webhook_configured_sends_nothing() -> None:
    """And makes no request at all — an empty setting is not an error."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with _client(handler) as http:
        assert await send_to_teams(http, "", title="Anything") is False
    assert called is False
