"""AI employees in Teams: the pieces that need no Microsoft, no database, no model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.msteams import bridge
from app.msteams.graph import GraphAccountError, decrypt, encrypt
from app.msteams.worker import mentions, new_messages, when

LUNA = "luna-oid"


def _msg(at: str, who: str, text: str = "hi", *, mention: bool = False, kind: str = "message") -> dict:
    message = {
        "createdDateTime": at,
        "messageType": kind,
        "from": {"user": {"id": who}},
        "body": {"contentType": "html", "content": text},
    }
    if mention:
        message["mentions"] = [{"mentioned": {"user": {"id": LUNA}}}]
    return message


def test_plain_text_drops_tags_and_mentions() -> None:
    body = '<p><at id="0">Luna (AI)</at> what is <b>due</b> this week?</p><p>thanks &amp; regards</p>'
    assert bridge.plain_text(body) == "what is due this week?\nthanks & regards"


@pytest.mark.parametrize(
    ("text", "answer"),
    [("yes", True), ("Yes please, go ahead", True), ("no", False), ("cancel it", False),
     ("maybe", None), ("yes no", None)],
)
def test_yes_or_no(text: str, answer: bool | None) -> None:
    assert bridge.yes_or_no(text) is answer


def test_to_html_is_safe_and_readable() -> None:
    out = bridge.to_html("**Due:** <script>x</script>\n- one\n- [two](https://hamdaz.com/x)")
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert out.startswith("<b>Due:</b>")
    assert "• one" in out and '<a href="https://hamdaz.com/x">two</a>' in out


def test_parse_events_reads_text_confirmations_and_screen_actions() -> None:
    body = (
        'event: run\ndata: {"run_id": "r1"}\n\n'
        'event: text\ndata: {"delta": "Hello "}\n\n'
        'event: text\ndata: {"delta": "there."}\n\n'
        'event: confirm\ndata: {"run_id": "r1", "actions": [{"label": "Apply for leave"}]}\n\n'
        'event: done\ndata: {"status": "awaiting_confirmation"}\n\n'
    )
    turn = bridge.parse_events(body)
    assert turn.text == "Hello there." and turn.run_id == "r1"
    assert turn.confirm == [{"label": "Apply for leave"}]
    reply = bridge.reply_for(turn)
    assert "Apply for leave" in reply and "**yes**" in reply
    assert bridge.reply_for(bridge.Turn(error="You may not.")) == "Sorry — You may not."


def test_only_new_messages_from_others_and_mentions_in_groups() -> None:
    since = when("2026-10-06T10:00:00Z")
    messages = [
        _msg("2026-10-06T10:05:00.12Z", "sebin", "second"),
        _msg("2026-10-06T10:01:00Z", "sebin", "first"),
        _msg("2026-10-06T09:59:00Z", "sebin", "before connecting"),
        _msg("2026-10-06T10:02:00Z", LUNA, "my own reply"),
        _msg("2026-10-06T10:03:00Z", "sebin", "system", kind="systemEventMessage"),
    ]
    picked = new_messages(messages, since=since, account_id=LUNA, group=False)
    assert [plain for plain in (bridge.plain_text(m["body"]["content"]) for m in picked)] == ["first", "second"]

    group = [_msg("2026-10-06T10:05:00Z", "sebin", "not to me"), _msg("2026-10-06T10:06:00Z", "sebin", "@luna", mention=True)]
    picked = new_messages(group, since=since, account_id=LUNA, group=True)
    assert len(picked) == 1 and mentions(picked[0], LUNA)


def test_when_reads_graph_and_python_times() -> None:
    assert when("2026-10-06T10:05:00.123Z") == datetime(2026, 10, 6, 10, 5, 0, 123000, tzinfo=UTC)
    assert when(None) is None and when("not a time") is None


def test_refresh_token_is_encrypted_and_tamper_evident() -> None:
    settings = get_settings()
    sealed = encrypt(settings, "refresh-token-value")
    assert "refresh-token-value" not in sealed
    assert decrypt(settings, sealed) == "refresh-token-value"
    with pytest.raises(GraphAccountError) as raised:
        decrypt(settings, sealed[:-4] + "AAAA")
    assert raised.value.reconnect


def test_the_test_lock_answers_only_the_listed_sender_one_to_one() -> None:
    from types import SimpleNamespace

    from app.msteams.worker import allowed

    settings = get_settings().model_copy()
    settings.ai_teams_test_senders = " Krishnendu@hamdaz.com "
    krishnendu = SimpleNamespace(email="krishnendu@hamdaz.com")
    someone_else = SimpleNamespace(email="jasna@hamdaz.com")
    one_to_one, group = {"chatType": "oneOnOne"}, {"chatType": "group"}

    assert allowed(settings, krishnendu, one_to_one)
    assert not allowed(settings, krishnendu, group)
    assert not allowed(settings, someone_else, one_to_one)
    assert not allowed(settings, None, one_to_one)  # not even "please sign in"

    settings.ai_teams_test_senders = ""
    assert allowed(settings, someone_else, group)


async def test_under_the_test_lock_only_the_listed_persons_chat_is_read() -> None:
    from datetime import timedelta
    from types import SimpleNamespace

    from app.msteams.worker import TeamsWorker

    sebin, krish = "sebin-oid", "krish-oid"
    their_chat = f"19:{krish}_{sebin}@unq.gbl.spaces"
    connected = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
    touched: list[str] = []

    class FakeGraph:
        async def recent_chats(self, token):
            raise AssertionError("the chat list must not be read under the test lock")

        async def chat_exists(self, token, chat_id):
            touched.append(chat_id)
            return chat_id == their_chat

        async def messages(self, token, chat_id):
            touched.append(f"messages:{chat_id}")
            return [
                _msg((connected + timedelta(minutes=5)).isoformat(), krish, "hi luna"),
                _msg((connected + timedelta(minutes=6)).isoformat(), sebin, "my own words"),
                _msg((connected - timedelta(minutes=5)).isoformat(), krish, "before connecting"),
            ]

    class FakeSession:
        async def scalars(self, _query):
            return SimpleNamespace(all=lambda: [krish])

        async def commit(self):
            pass

    settings = get_settings().model_copy()
    settings.ai_teams_test_senders = "krishnendu@hamdaz.com"
    worker = TeamsWorker(factory=None, settings=settings, graph=FakeGraph(), caller=None)
    answered: list[str] = []

    async def fake_answer(session, employee, account, token, chat, burst):
        answered.append(" / ".join(bridge.plain_text(m["body"]["content"]) for m in burst))
        return True

    worker.answer = fake_answer
    account = SimpleNamespace(entra_object_id=sebin, connected_at=connected, watermarks={}, last_poll_at=None, error=None)
    await worker.poll_test(FakeSession(), SimpleNamespace(name="Luna"), account, "token")

    assert answered == ["hi luna"]
    assert set(touched) <= {f"19:{sebin}_{krish}@unq.gbl.spaces", their_chat, f"messages:{their_chat}"}
    assert account.watermarks[f"_chat:{krish}"] == their_chat
    # Second look: nothing new, nothing answered again.
    answered.clear()
    await worker.poll_test(FakeSession(), SimpleNamespace(name="Luna"), account, "token")
    assert answered == []


def test_a_burst_is_one_turn() -> None:
    from app.msteams.worker import bursts

    a1, a2, b1, a3 = _msg("t1", "a", "hlo"), _msg("t2", "a", "hi"), _msg("t3", "b", "x"), _msg("t4", "a", "tasks?")
    assert [[m["body"]["content"] for m in g] for g in bursts([a1, a2, b1, a3])] == [["hlo", "hi"], ["x"], ["tasks?"]]


def test_tables_become_html_tables() -> None:
    md = "Your tasks:\n\n| # | Task | Due |\n|---|------|-----|\n| 32 | **Valves** RFQ | 3 Oct |\n| 56 | Cables | 9 Oct |\n\nAnything else?"
    out = bridge.to_html(md)
    assert "<table>" in out and "<th>Task</th>" in out
    assert "<td><b>Valves</b> RFQ</td>" in out and "<td>9 Oct</td>" in out
    assert "|" not in out and out.endswith("Anything else?")
