"""The assistant over HTTP: who is refused, and what a whole turn actually does.

The model is a stub. What is exercised for real is everything around it — the
admission gates, the tool set a person is given, the loop, the confirmation
pause and resume, the event log, and above all that a tool call goes through
the app's own routes and is refused there exactly as it would be from a
browser. That last property is the assistant's entire security model, so it is
tested by having the assistant attempt something the caller may not do.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.assistant import service
from app.assistant.agent import Assistant
from app.assistant.catalogue import (
    DEFAULT_MODEL,
    DEFAULT_SPEECH_MODEL,
    DEFAULT_VOICE,
    MODELS_BY_KEY,
    VOICE_INSTRUCTIONS,
    VOICE_MAX_CHARS,
    VOICES,
)
from app.assistant.executor import ToolExecutor
from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.models.assistant import EventKind, RunStatus
from app.proposals.analytics import WorkloadCache
from app.proposals.oversight import TeamTasksCache
from app.roles import service as roles_service
from app.zoho.cache import QuoteCache

SESSION_COOKIE = "hamdaz_session"

#: What the stubbed mint hands back, so a test can tell it through.
FAKE_SECRET = "ek_test_secret"


# ── standing in for OpenAI ─────────────────────────────────────────────


def _usage(input_tokens: int = 1000, output_tokens: int = 100, cached: int = 0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached, cache_write_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


def _completed(**kw):
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(status="completed", usage=_usage(**kw), error=None),
    )


def says(text: str) -> list:
    """A turn where the model just answers."""
    return [
        SimpleNamespace(type="response.output_text.delta", delta=text),
        SimpleNamespace(
            type="response.output_item.done",
            item={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        ),
        _completed(),
    ]


def calls(name: str, arguments: dict, call_id: str = "call_1") -> list:
    """A turn where the model asks for one tool."""
    return [
        SimpleNamespace(
            type="response.output_item.done",
            item={
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            },
        ),
        _completed(),
    ]


class FakeStream:
    def __init__(self, events: list) -> None:
        self._events = events

    async def __aiter__(self):
        for event in self._events:
            yield event


class FakeLLM:
    """Plays a scripted sequence of model turns and records what it was sent."""

    def __init__(self) -> None:
        self.script: list[list] = []
        self.calls: list[dict] = []
        self.spoken: list[dict] = []
        self.minted: list[dict] = []

    def plays(self, *turns: list) -> None:
        self.script = list(turns)

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        events = self.script.pop(0) if self.script else says("Nothing further.")
        return FakeStream(events)

    async def speak(self, text: str, **kwargs):
        """Stands in for the speech model. Records what it was asked to say."""
        self.spoken.append({"text": text, **kwargs})
        yield b"ID3fake-mp3-"
        yield text.encode()

    async def realtime_secret(self, **kwargs) -> tuple[str, int]:
        """Records what the session was minted with. That is the whole point:
        the tool list and instructions must be decided here, not by the client."""
        self.minted.append(kwargs)
        return FAKE_SECRET, 1788800000


def sse(text: str) -> list[tuple[str, dict]]:
    """Parse an event stream into (event name, data) pairs."""
    out: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        kind: str | None = None
        data: dict = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if kind:
            out.append((kind, data))
    return out


def only(events: list[tuple[str, dict]], kind: str) -> list[dict]:
    return [data for name, data in events if name == kind]


def text_of(events: list[tuple[str, dict]]) -> str:
    return "".join(d["delta"] for d in only(events, "text"))


# ── fixtures ───────────────────────────────────────────────────────────


class StubMailer:
    """Stands in for the leave mailer.

    Needed because a tool call travels the real route, and that route resolves
    ``app.state.leave_mailer`` before the handler runs — whether or not mail is
    switched on. Without it a leave write is a 500 rather than a booking, which
    is exactly the sort of thing running tools through the real routes is meant
    to surface.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_request(self, request, recipients: list[str]) -> dict:
        self.sent.append({"request_id": str(request.id), "recipients": list(recipients)})
        return {"id": "stub-message"}


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def wired(client, engine, llm: FakeLLM) -> FakeLLM:
    """Put the assistant on the test app, calling that same app for its tools.

    The executor is pointed at the very app under test, which is the whole
    point: a tool call travels the real route and meets the real guard.
    """
    app = client._transport.app
    settings = get_settings()
    # Collaborators the tool routes reach for. The conftest client sets only
    # what its own tests need; a tool call goes through routes those tests
    # never exercise, so the rest are supplied here.
    app.state.leave_mailer = StubMailer()
    app.state.workload_cache = WorkloadCache()
    app.state.team_tasks_cache = TeamTasksCache()
    app.state.quote_cache = QuoteCache()
    app.state.assistant = Assistant(
        llm,
        ToolExecutor(
            app,
            api_prefix=settings.api_prefix,
            cookie_name=settings.session_cookie_name,
            timeout=30.0,
        ),
        # A turn opens its own sessions, so they must reach the test database.
        async_sessionmaker(engine, expire_on_commit=False),
    )
    return llm


@pytest.fixture
async def setup(db):
    await roles_service.seed_system_roles(db)
    await service.seed_models(db)
    await service.seed_policies(db)
    # On, and open to everyone, so each test says what it is actually about.
    await service.update_settings(
        db, actor_id=None, changes={"enabled": True, "audience_mode": "everyone"}
    )
    await db.commit()


async def _make(db, email: str, *role_keys: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in role_keys:
        await roles_service.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)
    await db.commit()
    return user


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
async def person(db, setup):
    return await _make(db, "amina@hamdaz.com")


@pytest.fixture
async def boss(db, setup):
    return await _make(db, "boss@hamdaz.com", "super_admin")


@pytest.fixture
async def manager(db, setup):
    return await _make(db, "manager@hamdaz.com", "manager")


async def _chat(client, user) -> str:
    response = await _as(client, user).post("/api/v1/assistant/conversations", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _send(client, conversation_id: str, text: str):
    return await client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/messages", json={"text": text}
    )


# ── who may reach it at all ────────────────────────────────────────────


async def test_the_chat_needs_a_session(client, setup) -> None:
    assert (await client.get("/api/v1/assistant/status")).status_code == 401


async def test_status_says_when_the_assistant_is_switched_off(client, db, person) -> None:
    await service.update_settings(db, actor_id=None, changes={"enabled": False})
    await db.commit()
    body = (await _as(client, person).get("/api/v1/assistant/status")).json()
    assert body["enabled"] is False
    assert body["admitted"] is False
    assert body["code"] == "disabled"
    assert body["modules"] == []


async def test_status_says_when_somebody_has_not_been_released_yet(client, db, person) -> None:
    await service.update_settings(db, actor_id=None, changes={"audience_mode": "allow_list"})
    await db.commit()
    body = (await _as(client, person).get("/api/v1/assistant/status")).json()
    assert body["admitted"] is False
    assert body["code"] == "not_released"


async def test_releasing_a_person_lets_them_in(client, db, person) -> None:
    await service.update_settings(db, actor_id=None, changes={"audience_mode": "allow_list"})
    await service.create_rule(
        db, actor_id=None, subject_type="user", subject=person.email, effect="allow"
    )
    await db.commit()
    body = (await _as(client, person).get("/api/v1/assistant/status")).json()
    assert body["admitted"] is True
    assert body["model"]


async def test_status_lists_only_the_tools_this_person_gets(client, person) -> None:
    body = (await _as(client, person).get("/api/v1/assistant/status")).json()
    modules = {m["key"] for m in body["modules"]}
    assert "leave" in modules  # open to everyone
    assert "roles" not in modules  # needs an admin role
    every_tool = [t for m in body["modules"] for t in m["tools"]]
    assert every_tool and all(t["kind"] == "read" for t in every_tool)


async def test_a_blocked_person_cannot_start_a_chat(client, db, person) -> None:
    await service.create_rule(
        db, actor_id=None, subject_type="user", subject=person.email, effect="block"
    )
    await db.commit()
    response = await _as(client, person).post("/api/v1/assistant/conversations", json={})
    assert response.status_code == 403


async def test_a_conversation_is_private_to_its_owner(client, db, person, wired) -> None:
    other = await _make(db, "bilal@hamdaz.com")
    conversation_id = await _chat(client, person)
    response = await _as(client, other).get(f"/api/v1/assistant/conversations/{conversation_id}")
    assert response.status_code == 404


# ── a whole turn ───────────────────────────────────────────────────────


async def test_a_plain_answer_is_streamed_and_kept(client, person, wired) -> None:
    wired.plays(says("You have 12 days of leave left."))
    conversation_id = await _chat(client, person)

    response = await _send(client, conversation_id, "How much leave do I have?")
    assert response.status_code == 200
    events = sse(response.text)

    assert text_of(events) == "You have 12 days of leave left."
    done = only(events, "done")[0]
    assert done["status"] == RunStatus.COMPLETED

    kept = (await client.get(f"/api/v1/assistant/conversations/{conversation_id}")).json()
    assert [m["role"] for m in kept["messages"]] == ["user", "assistant"]
    assert kept["messages"][1]["content"] == "You have 12 days of leave left."


async def test_the_question_reaches_the_model(client, person, wired) -> None:
    """Without this the model answers with no idea what was asked."""
    wired.plays(says("Sure."))
    conversation_id = await _chat(client, person)
    await _send(client, conversation_id, "Who is off next week?")

    sent = wired.calls[0]["input"]
    assert {"role": "user", "content": "Who is off next week?"} in sent


async def test_the_model_is_only_offered_tools_this_person_has(client, person, wired) -> None:
    wired.plays(says("Sure."))
    conversation_id = await _chat(client, person)
    await _send(client, conversation_id, "hello")

    sent = wired.calls[0]["tools"]
    offered = {t["name"] for t in sent if t.get("type") == "function"}
    assert "leave__mine" in offered
    assert "roles__grant" not in offered  # an admin write, twice over
    assert not any(name.startswith("roles__") for name in offered)

    # The rarely-used tools ride along deferred, with a search tool so the model
    # can fetch one when it needs it. Everyday tools stay loaded.
    assert any(t.get("type") == "tool_search" for t in sent)
    loaded = {t["name"] for t in sent if t.get("type") == "function" and not t.get("defer_loading")}
    assert "leave__mine" in loaded
    assert "leave__calendar" in loaded


async def test_a_read_tool_runs_against_the_real_route(client, person, wired) -> None:
    wired.plays(
        calls("leave__mine", {}),
        says("You have no leave requests yet."),
    )
    conversation_id = await _chat(client, person)
    events = sse((await _send(client, conversation_id, "my leave?")).text)

    started = only(events, "tool_call")
    assert [c["tool_key"] for c in started] == ["leave.mine"]
    result = only(events, "tool_result")[0]
    assert result["ok"] is True and result["status"] == 200

    # The result went back to the model as a tool output, not as prose.
    second = wired.calls[1]["input"]
    outputs = [i for i in second if i.get("type") == "function_call_output"]
    assert len(outputs) == 1
    assert json.loads(outputs[0]["output"]) == []


async def test_the_run_log_records_every_step(client, boss, wired) -> None:
    wired.plays(calls("leave__mine", {}), says("Nothing outstanding."))
    conversation_id = await _chat(client, boss)
    events = sse((await _send(client, conversation_id, "my leave?")).text)
    run_id = only(events, "done")[0]["run_id"]

    log = (await _as(client, boss).get(f"/api/v1/assistant/admin/runs/{run_id}")).json()
    kinds = [e["kind"] for e in log["events"]]
    assert EventKind.USER_MESSAGE in kinds
    assert kinds.count(EventKind.TOOL_CALL) == 1
    assert kinds.count(EventKind.TOOL_RESULT) == 1
    assert kinds.count(EventKind.MODEL_USAGE) == 2  # one per round
    assert [e["seq"] for e in log["events"]] == sorted(e["seq"] for e in log["events"])
    assert log["status"] == RunStatus.COMPLETED
    assert log["tool_calls"] == 1 and log["rounds"] == 2


async def test_usage_and_cost_are_recorded(client, boss, wired) -> None:
    wired.plays(says("Done."))
    conversation_id = await _chat(client, boss)
    events = sse((await _send(client, conversation_id, "hello")).text)
    done = only(events, "done")[0]

    assert done["input_tokens"] == 1000 and done["output_tokens"] == 100
    # Priced from the catalogue rather than a hardcoded figure, so changing the
    # shipped default model does not silently turn this into a different test.
    priced = MODELS_BY_KEY[DEFAULT_MODEL]
    expected = float(
        (Decimal(1000) * priced.input_price + Decimal(100) * priced.output_price)
        / Decimal(1_000_000)
    )
    assert float(done["cost_usd"]) == pytest.approx(expected)


async def test_a_tool_the_person_may_not_use_comes_back_as_refused(client, person, wired) -> None:
    """The model asking for something off its list must not reach the route."""
    wired.plays(calls("roles__assignments", {}), says("You are not allowed to see that."))
    conversation_id = await _chat(client, person)
    events = sse((await _send(client, conversation_id, "who is an admin?")).text)

    assert only(events, "tool_call") == []
    second = wired.calls[1]["input"]
    output = next(i for i in second if i.get("type") == "function_call_output")
    assert json.loads(output["output"])["status"] == 403


async def test_a_route_refusal_is_reported_rather_than_hidden(client, db, person, wired) -> None:
    """leave.queue is HR-only. The assistant must be refused exactly as a browser is."""
    await service.update_module_policy(
        db, "leave", actor_id=None, changes={"write_enabled": False}
    )
    await db.commit()
    wired.plays(calls("leave__queue", {}), says("You are not on the HR team."))
    conversation_id = await _chat(client, person)
    events = sse((await _send(client, conversation_id, "show every leave request")).text)

    result = only(events, "tool_result")[0]
    assert result["ok"] is False and result["status"] == 403
    output = next(
        i for i in wired.calls[1]["input"] if i.get("type") == "function_call_output"
    )
    assert json.loads(output["output"])["status"] == 403


# ── writes and confirmation ────────────────────────────────────────────


@pytest.fixture
async def leave_writes(db, setup):
    await service.update_module_policy(
        db, "leave", actor_id=None, changes={"write_enabled": True}
    )
    await db.commit()


async def test_a_write_pauses_for_confirmation(client, person, leave_writes, wired) -> None:
    wired.plays(
        calls(
            "leave__request",
            {
                "leave_type": "annual",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "reason": None,
            },
        )
    )
    conversation_id = await _chat(client, person)
    events = sse((await _send(client, conversation_id, "book me 1-2 December")).text)

    ask = only(events, "confirm")[0]
    assert [a["tool_key"] for a in ask["actions"]] == ["leave.request"]
    assert ask["actions"][0]["arguments"]["start_date"] == "2026-12-01"
    assert only(events, "done")[0]["status"] == RunStatus.AWAITING_CONFIRMATION
    # Nothing ran: the write is still waiting on the person.
    assert only(events, "tool_result") == []

    kept = (await client.get(f"/api/v1/assistant/conversations/{conversation_id}")).json()
    assert kept["pending"]["actions"][0]["tool_key"] == "leave.request"


async def test_confirming_runs_the_write(client, person, leave_writes, wired) -> None:
    wired.plays(
        calls(
            "leave__request",
            {
                "leave_type": "annual",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "reason": None,
            },
        ),
        says("Booked, and it was approved."),
    )
    conversation_id = await _chat(client, person)
    first = sse((await _send(client, conversation_id, "book me 1-2 December")).text)
    run_id = only(first, "confirm")[0]["run_id"]

    response = await client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/confirm",
        json={"run_id": run_id, "approved": True},
    )
    assert response.status_code == 200
    events = sse(response.text)
    result = only(events, "tool_result")[0]
    assert result["ok"] is True and result["status"] == 201
    assert text_of(events) == "Booked, and it was approved."

    # It really happened: the leave module has the request.
    mine = (await client.get("/api/v1/leave/requests/me")).json()
    assert [r["start_date"] for r in mine] == ["2026-12-01"]


async def test_declining_does_not_run_the_write(client, person, leave_writes, wired) -> None:
    wired.plays(
        calls(
            "leave__request",
            {
                "leave_type": "annual",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "reason": None,
            },
        ),
        says("Left it alone."),
    )
    conversation_id = await _chat(client, person)
    first = sse((await _send(client, conversation_id, "book me 1-2 December")).text)
    run_id = only(first, "confirm")[0]["run_id"]

    events = sse(
        (
            await client.post(
                f"/api/v1/assistant/conversations/{conversation_id}/confirm",
                json={"run_id": run_id, "approved": False},
            )
        ).text
    )
    assert text_of(events) == "Left it alone."
    assert (await client.get("/api/v1/leave/requests/me")).json() == []

    # And the model was told, so it does not claim the booking was made.
    output = next(
        i for i in wired.calls[1]["input"] if i.get("type") == "function_call_output"
    )
    assert "declined" in output["output"].lower()


async def test_a_write_runs_straight_away_when_confirmation_is_off(
    client, db, person, leave_writes, wired
) -> None:
    await service.update_module_policy(
        db, "leave", actor_id=None, changes={"confirm_writes": False}
    )
    await db.commit()
    wired.plays(
        calls(
            "leave__request",
            {
                "leave_type": "annual",
                "start_date": "2026-12-08",
                "end_date": "2026-12-08",
                "reason": None,
            },
        ),
        says("Booked."),
    )
    conversation_id = await _chat(client, person)
    events = sse((await _send(client, conversation_id, "book 8 December")).text)

    assert only(events, "confirm") == []
    assert only(events, "tool_result")[0]["status"] == 201


async def test_a_second_message_is_refused_while_one_is_waiting(
    client, person, leave_writes, wired
) -> None:
    wired.plays(
        calls(
            "leave__request",
            {
                "leave_type": "annual",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "reason": None,
            },
        )
    )
    conversation_id = await _chat(client, person)
    await _send(client, conversation_id, "book me 1-2 December")

    response = await _send(client, conversation_id, "actually never mind")
    assert response.status_code == 409
    assert "confirm" in response.json()["detail"].lower()


async def test_confirming_a_run_that_is_not_waiting_is_refused(
    client, person, wired
) -> None:
    wired.plays(says("Nothing to do."))
    conversation_id = await _chat(client, person)
    events = sse((await _send(client, conversation_id, "hello")).text)
    run_id = only(events, "done")[0]["run_id"]

    response = await client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/confirm",
        json={"run_id": run_id, "approved": True},
    )
    assert response.status_code == 409


# ── limits ─────────────────────────────────────────────────────────────


async def test_the_round_limit_ends_a_turn_that_will_not_settle(
    client, person, wired, db
) -> None:
    await service.update_settings(db, actor_id=None, changes={"max_tool_rounds": 2})
    await db.commit()
    wired.plays(
        calls("leave__mine", {}, "a"),
        calls("leave__mine", {}, "b"),
        calls("leave__mine", {}, "c"),
    )
    conversation_id = await _chat(client, person)
    events = sse((await _send(client, conversation_id, "loop please")).text)

    assert only(events, "done")[0]["status"] == RunStatus.FAILED
    assert "stopped" in only(events, "error")[0]["message"].lower()


# ── the control panel ──────────────────────────────────────────────────


ADMIN_READS = [
    "/api/v1/assistant/admin/settings",
    "/api/v1/assistant/admin/models",
    "/api/v1/assistant/admin/policies",
    "/api/v1/assistant/admin/rules",
    "/api/v1/assistant/admin/runs",
    "/api/v1/assistant/admin/analytics",
    "/api/v1/assistant/admin/catalogue",
]


@pytest.mark.parametrize("path", ADMIN_READS)
async def test_the_control_panel_needs_a_session(client, setup, path: str) -> None:
    assert (await client.get(path)).status_code == 401


@pytest.mark.parametrize("path", ADMIN_READS)
async def test_an_ordinary_person_cannot_reach_the_control_panel(
    client, person, path: str
) -> None:
    assert (await _as(client, person).get(path)).status_code == 403


@pytest.mark.parametrize("path", ADMIN_READS)
async def test_even_a_manager_cannot_reach_the_control_panel(
    client, manager, path: str
) -> None:
    """Deliberately narrower than ADMIN_ROLES: running teams is not the same
    question as deciding what an assistant may do on everybody's behalf."""
    assert (await _as(client, manager).get(path)).status_code == 403


async def test_a_super_admin_reads_the_settings(client, boss) -> None:
    body = (await _as(client, boss).get("/api/v1/assistant/admin/settings")).json()
    assert body["enabled"] is True
    assert body["model_key"]
    assert "openai_configured" in body


async def test_a_super_admin_changes_the_model(client, boss) -> None:
    response = await _as(client, boss).patch(
        "/api/v1/assistant/admin/settings", json={"model_key": "gpt-5.6-luna"}
    )
    assert response.status_code == 200
    assert response.json()["model_key"] == "gpt-5.6-luna"
    models = (await client.get("/api/v1/assistant/admin/models")).json()
    assert next(m for m in models if m["key"] == "gpt-5.6-luna")["active"] is True


async def test_choosing_a_model_that_does_not_exist_is_refused(client, boss) -> None:
    response = await _as(client, boss).patch(
        "/api/v1/assistant/admin/settings", json={"model_key": "gpt-imaginary"}
    )
    assert response.status_code == 404


async def test_a_manager_cannot_change_the_settings(client, manager) -> None:
    response = await _as(client, manager).patch(
        "/api/v1/assistant/admin/settings", json={"enabled": False}
    )
    assert response.status_code == 403


async def test_the_policy_matrix_shows_every_module_and_tool(client, boss) -> None:
    matrix = (await _as(client, boss).get("/api/v1/assistant/admin/policies")).json()
    keys = {m["module_key"] for m in matrix}
    assert {"leave", "teams", "roles"} <= keys
    leave = next(m for m in matrix if m["module_key"] == "leave")
    assert leave["write_enabled"] is False
    request = next(t for t in leave["tools"] if t["tool_key"] == "leave.request")
    assert request["kind"] == "write"
    assert request["effective_enabled"] is False


async def test_turning_on_writes_shows_in_the_matrix_and_in_the_chat(
    client, boss, wired
) -> None:
    response = await _as(client, boss).patch(
        "/api/v1/assistant/admin/policies/modules/leave", json={"write_enabled": True}
    )
    assert response.status_code == 200
    leave = next(m for m in response.json() if m["module_key"] == "leave")
    request = next(t for t in leave["tools"] if t["tool_key"] == "leave.request")
    assert request["effective_enabled"] is True and request["effective_confirm"] is True

    status = (await client.get("/api/v1/assistant/status")).json()
    tools = [t for m in status["modules"] if m["key"] == "leave" for t in m["tools"]]
    assert any(t["key"] == "leave.request" and t["requires_confirmation"] for t in tools)


async def test_turning_off_one_tool_removes_it_from_the_chat(client, boss) -> None:
    await _as(client, boss).patch(
        "/api/v1/assistant/admin/policies/tools/leave.calendar", json={"enabled": False}
    )
    status = (await client.get("/api/v1/assistant/status")).json()
    tools = {t["key"] for m in status["modules"] for t in m["tools"]}
    assert "leave.calendar" not in tools
    assert "leave.mine" in tools


async def test_a_rule_can_be_created_listed_and_deleted(client, boss, db) -> None:
    await _make(db, "amina@hamdaz.com")
    created = await _as(client, boss).post(
        "/api/v1/assistant/admin/rules",
        json={"subject_type": "user", "subject": "amina@hamdaz.com", "effect": "allow"},
    )
    assert created.status_code == 201
    rule_id = created.json()["id"]

    listed = (await client.get("/api/v1/assistant/admin/rules")).json()
    assert [r["id"] for r in listed] == [rule_id]

    assert (
        await client.delete(f"/api/v1/assistant/admin/rules/{rule_id}")
    ).status_code == 204
    assert (await client.get("/api/v1/assistant/admin/rules")).json() == []


async def test_a_rule_naming_nobody_is_refused(client, boss) -> None:
    response = await _as(client, boss).post(
        "/api/v1/assistant/admin/rules",
        json={"subject_type": "user", "subject": "ghost@hamdaz.com", "effect": "allow"},
    )
    assert response.status_code == 404


async def test_runs_and_analytics_reflect_a_real_turn(client, boss, wired) -> None:
    wired.plays(calls("leave__mine", {}), says("Nothing outstanding."))
    conversation_id = await _chat(client, boss)
    await _send(client, conversation_id, "my leave?")

    runs = (await _as(client, boss).get("/api/v1/assistant/admin/runs")).json()
    assert runs["total"] == 1
    assert runs["runs"][0]["user_email"] == "boss@hamdaz.com"
    assert runs["runs"][0]["status"] == RunStatus.COMPLETED

    figures = (await client.get("/api/v1/assistant/admin/analytics")).json()
    assert figures["totals"]["runs"] == 1
    assert figures["totals"]["tool_calls"] == 1
    assert figures["totals"]["people"] == 1
    assert float(figures["totals"]["cost_usd"]) > 0
    assert [b["key"] for b in figures["by_tool"]] == ["leave.mine"]
    assert figures["by_model"][0]["key"] == DEFAULT_MODEL


async def test_analytics_counts_a_refused_action(client, boss, person, wired) -> None:
    wired.plays(calls("roles__assignments", {}), says("Not allowed."))
    conversation_id = await _chat(client, person)
    await _send(client, conversation_id, "who is an admin?")

    figures = (
        await _as(client, boss).get("/api/v1/assistant/admin/analytics")
    ).json()
    assert figures["totals"]["refused_by_policy"] == 1


async def test_analytics_counts_confirmations(client, boss, leave_writes, wired) -> None:
    wired.plays(
        calls(
            "leave__request",
            {
                "leave_type": "annual",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "reason": None,
            },
        ),
        says("Booked."),
    )
    conversation_id = await _chat(client, boss)
    first = sse((await _send(client, conversation_id, "book 1-2 December")).text)
    run_id = only(first, "confirm")[0]["run_id"]
    await client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/confirm",
        json={"run_id": run_id, "approved": True},
    )

    figures = (await client.get("/api/v1/assistant/admin/analytics")).json()
    assert figures["totals"]["confirmations_requested"] == 1
    assert figures["totals"]["confirmations_approved"] == 1
    assert figures["totals"]["confirmations_declined"] == 0


async def test_a_super_admin_can_cancel_a_waiting_turn(
    client, boss, leave_writes, wired
) -> None:
    wired.plays(
        calls(
            "leave__request",
            {
                "leave_type": "annual",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "reason": None,
            },
        )
    )
    conversation_id = await _chat(client, boss)
    events = sse((await _send(client, conversation_id, "book 1-2 December")).text)
    run_id = only(events, "confirm")[0]["run_id"]

    live = (await client.get("/api/v1/assistant/admin/runs/live")).json()
    assert [r["id"] for r in live] == [run_id]

    cancelled = await client.post(f"/api/v1/assistant/admin/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == RunStatus.CANCELLED
    assert (await client.get("/api/v1/assistant/admin/runs/live")).json() == []
    # And the write never happened.
    assert (await client.get("/api/v1/leave/requests/me")).json() == []


# ── the voice ──────────────────────────────────────────────────────────


@pytest.fixture
async def voice_on(db, setup):
    await service.update_settings(db, actor_id=None, changes={"voice_enabled": True})
    await db.commit()


async def test_the_voice_list_needs_a_session(client, setup) -> None:
    assert (await client.get("/api/v1/assistant/voices")).status_code == 401


async def test_the_voice_list_marks_the_configured_one(client, person) -> None:
    body = (await _as(client, person).get("/api/v1/assistant/voices")).json()
    assert len(body["voices"]) == len(VOICES)
    active = [v["key"] for v in body["voices"] if v["active"]]
    assert active == [body["voice"]]
    assert body["instructions"], "the shipped steering should be reported, not blank"


async def test_status_names_the_voice_only_when_it_is_on(client, db, person) -> None:
    off = (await _as(client, person).get("/api/v1/assistant/status")).json()
    assert off["voice_enabled"] is False and off["voice"] is None

    await service.update_settings(db, actor_id=None, changes={"voice_enabled": True})
    await db.commit()
    on = (await client.get("/api/v1/assistant/status")).json()
    assert on["voice_enabled"] is True and on["voice"] == DEFAULT_VOICE


async def test_speaking_is_refused_while_the_voice_is_off(client, person, wired) -> None:
    response = await _as(client, person).post(
        "/api/v1/assistant/speech", json={"text": "Hello there."}
    )
    assert response.status_code == 409
    assert "switched off" in response.json()["detail"]


async def test_speaking_streams_audio(client, person, voice_on, wired) -> None:
    response = await _as(client, person).post(
        "/api/v1/assistant/speech", json={"text": "You have three tasks open."}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content.startswith(b"ID3")
    assert b"You have three tasks open." in response.content


async def test_speaking_uses_the_configured_voice_and_steering(
    client, person, voice_on, wired
) -> None:
    await _as(client, person).post("/api/v1/assistant/speech", json={"text": "Ready."})
    asked = wired.spoken[-1]
    assert asked["voice"] == DEFAULT_VOICE
    assert asked["model"] == DEFAULT_SPEECH_MODEL
    # The steering is what separates this from a flat reading, so it must arrive.
    assert asked["instructions"] == VOICE_INSTRUCTIONS


async def test_an_administrators_wording_replaces_the_shipped_one(
    client, db, person, voice_on, wired
) -> None:
    await service.update_settings(
        db, actor_id=None, changes={"voice_instructions": "Speak slowly and formally."}
    )
    await db.commit()
    await _as(client, person).post("/api/v1/assistant/speech", json={"text": "Ready."})
    assert wired.spoken[-1]["instructions"] == "Speak slowly and formally."


async def test_clearing_the_wording_restores_the_shipped_one(
    client, db, person, voice_on, wired
) -> None:
    """An empty box means "use the default", not "read this with no steering"."""
    await service.update_settings(db, actor_id=None, changes={"voice_instructions": "   "})
    await db.commit()
    await _as(client, person).post("/api/v1/assistant/speech", json={"text": "Ready."})
    assert wired.spoken[-1]["instructions"] == VOICE_INSTRUCTIONS


async def test_an_ordinary_person_cannot_pick_a_different_voice(
    client, person, voice_on, wired
) -> None:
    """Otherwise the endpoint is a voice playground rather than the assistant's."""
    other = next(v for v in VOICES if v != DEFAULT_VOICE)
    response = await _as(client, person).post(
        "/api/v1/assistant/speech", json={"text": "Ready.", "voice": other}
    )
    assert response.status_code == 403


async def test_a_super_admin_can_sample_another_voice(client, boss, voice_on, wired) -> None:
    other = next(v for v in VOICES if v != DEFAULT_VOICE)
    response = await _as(client, boss).post(
        "/api/v1/assistant/speech", json={"text": "Ready.", "voice": other}
    )
    assert response.status_code == 200
    assert wired.spoken[-1]["voice"] == other


async def test_an_unknown_voice_is_refused(client, boss, voice_on, wired) -> None:
    response = await _as(client, boss).post(
        "/api/v1/assistant/speech", json={"text": "Ready.", "voice": "gandalf"}
    )
    assert response.status_code == 400


async def test_a_blocked_person_cannot_use_the_voice(client, db, person, voice_on, wired) -> None:
    await service.create_rule(
        db, actor_id=None, subject_type="user", subject=person.email, effect="block"
    )
    await db.commit()
    response = await _as(client, person).post(
        "/api/v1/assistant/speech", json={"text": "Ready."}
    )
    assert response.status_code == 403


async def test_an_enormous_block_of_text_is_refused(client, person, voice_on, wired) -> None:
    """It bills per character and nobody listens to a document read aloud."""
    response = await _as(client, person).post(
        "/api/v1/assistant/speech", json={"text": "a" * (VOICE_MAX_CHARS + 1)}
    )
    assert response.status_code == 422


async def test_a_super_admin_changes_the_voice(client, boss) -> None:
    other = next(v for v in VOICES if v != DEFAULT_VOICE)
    response = await _as(client, boss).patch(
        "/api/v1/assistant/admin/settings", json={"voice": other, "voice_enabled": True}
    )
    assert response.status_code == 200
    assert response.json()["voice"] == other


async def test_a_voice_that_does_not_exist_is_refused(client, boss) -> None:
    response = await _as(client, boss).patch(
        "/api/v1/assistant/admin/settings", json={"voice": "gandalf"}
    )
    assert response.status_code == 400


async def test_a_speech_model_that_does_not_exist_is_refused(client, boss) -> None:
    response = await _as(client, boss).patch(
        "/api/v1/assistant/admin/settings", json={"voice_model": "singer-9000"}
    )
    assert response.status_code == 400


# ── the caches in front of the database ────────────────────────────────
#
# This database is a third of a second away and a turn used to read the whole
# configuration and the caller's whole permission picture before it started.
# Both are cached now, so what these tests protect is the invalidation: a super
# admin's change must be visible on the very next request, not a minute later.


async def test_an_admin_change_is_visible_immediately(client, boss, wired) -> None:
    """The cache must not outlive the setting it holds."""
    before = (await _as(client, boss).get("/api/v1/assistant/status")).json()
    assert before["admitted"] is True

    await client.patch("/api/v1/assistant/admin/settings", json={"enabled": False})

    after = (await client.get("/api/v1/assistant/status")).json()
    assert after["enabled"] is False
    assert after["admitted"] is False


async def test_turning_a_tool_off_takes_effect_at_once(client, boss) -> None:
    first = (await _as(client, boss).get("/api/v1/assistant/status")).json()
    assert "leave.calendar" in {t["key"] for m in first["modules"] for t in m["tools"]}

    await client.patch(
        "/api/v1/assistant/admin/policies/tools/leave.calendar", json={"enabled": False}
    )

    second = (await client.get("/api/v1/assistant/status")).json()
    assert "leave.calendar" not in {t["key"] for m in second["modules"] for t in m["tools"]}


async def test_a_new_block_rule_takes_effect_at_once(client, db, boss, person) -> None:
    assert (await _as(client, person).get("/api/v1/assistant/status")).json()["admitted"] is True

    await _as(client, boss).post(
        "/api/v1/assistant/admin/rules",
        json={"subject_type": "user", "subject": person.email, "effect": "block"},
    )

    refused = (await _as(client, person).get("/api/v1/assistant/status")).json()
    assert refused["admitted"] is False
    assert refused["code"] == "blocked"


async def test_a_changed_voice_is_used_by_the_next_request(
    client, boss, voice_on, wired
) -> None:
    other = next(v for v in VOICES if v != DEFAULT_VOICE)
    await _as(client, boss).patch("/api/v1/assistant/admin/settings", json={"voice": other})
    await client.post("/api/v1/assistant/speech", json={"text": "Ready."})
    assert wired.spoken[-1]["voice"] == other


# ── spoken conversation ────────────────────────────────────────────────
#
# OpenAI runs this loop, not us, so what these protect is the boundary: the
# session is defined server-side, and every tool call comes back here to be
# checked again rather than being trusted because the client asked nicely.


@pytest.fixture
async def realtime_on(db, setup):
    await service.update_settings(db, actor_id=None, changes={"realtime_enabled": True})
    await db.commit()


async def test_a_spoken_session_needs_a_session_cookie(client, setup) -> None:
    assert (await client.post("/api/v1/assistant/realtime/session")).status_code == 401


async def test_a_spoken_session_is_refused_while_it_is_off(client, person, wired) -> None:
    response = await _as(client, person).post("/api/v1/assistant/realtime/session")
    assert response.status_code == 409
    assert "switched off" in response.json()["detail"]


async def test_a_blocked_person_gets_no_spoken_session(
    client, db, person, realtime_on, wired
) -> None:
    await service.create_rule(
        db, actor_id=None, subject_type="user", subject=person.email, effect="block"
    )
    await db.commit()
    response = await _as(client, person).post("/api/v1/assistant/realtime/session")
    assert response.status_code == 403


async def test_a_spoken_session_is_minted_with_this_persons_tools(
    client, person, realtime_on, wired
) -> None:
    response = await _as(client, person).post("/api/v1/assistant/realtime/session")
    assert response.status_code == 201
    body = response.json()

    assert body["client_secret"] == FAKE_SECRET
    assert body["model"] and body["voice"]
    offered = {t["tool_key"] for t in body["tools"]}
    assert "leave.mine" in offered
    # Admin-gated tools are not this person's, and writes are off in voice mode.
    assert not any(t.startswith("roles.") for t in offered)
    assert all(t["kind"] == "read" for t in body["tools"])


async def test_the_session_is_defined_server_side(client, person, realtime_on, wired) -> None:
    """The browser gets a key to a room it did not furnish."""
    await _as(client, person).post("/api/v1/assistant/realtime/session")
    minted = wired.minted[-1]
    names = {t["name"] for t in minted["tools"]}
    assert "leave__mine" in names
    assert not any(n.startswith("roles__") for n in names)
    # The spoken rules are part of what was minted, not something the client adds.
    assert "speaking out loud" in minted["instructions"]


async def test_writes_stay_out_of_voice_mode_until_turned_on(
    client, db, person, realtime_on, leave_writes, wired
) -> None:
    body = (await _as(client, person).post("/api/v1/assistant/realtime/session")).json()
    assert not any(t["kind"] == "write" for t in body["tools"])

    await service.update_settings(db, actor_id=None, changes={"realtime_writes_enabled": True})
    await db.commit()
    body = (await _as(client, person).post("/api/v1/assistant/realtime/session")).json()
    assert "leave.request" in {t["tool_key"] for t in body["tools"]}


async def test_a_spoken_tool_call_runs_against_the_real_route(
    client, person, realtime_on, wired
) -> None:
    started = (await _as(client, person).post("/api/v1/assistant/realtime/session")).json()
    response = await client.post(
        "/api/v1/assistant/realtime/call",
        json={"run_id": started["run_id"], "name": "leave__mine", "arguments": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["status"] == 200
    assert json.loads(body["output"]) == []


async def test_a_tool_this_person_lacks_is_refused_however_it_is_asked(
    client, person, realtime_on, wired
) -> None:
    """The client relays what the model wanted; it does not decide what may run."""
    started = (await _as(client, person).post("/api/v1/assistant/realtime/session")).json()
    response = await client.post(
        "/api/v1/assistant/realtime/call",
        json={"run_id": started["run_id"], "name": "roles__assignments", "arguments": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["status"] == 403


async def test_a_write_is_refused_in_voice_mode_when_writes_are_off(
    client, person, realtime_on, leave_writes, wired
) -> None:
    started = (await _as(client, person).post("/api/v1/assistant/realtime/session")).json()
    response = await client.post(
        "/api/v1/assistant/realtime/call",
        json={
            "run_id": started["run_id"],
            "name": "leave__request",
            "arguments": {
                "leave_type": "annual",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "reason": None,
            },
        },
    )
    assert response.json()["status"] == 403
    assert (await client.get("/api/v1/leave/requests/me")).json() == []


async def test_a_spoken_write_asks_before_it_runs(
    client, db, person, realtime_on, leave_writes, wired
) -> None:
    await service.update_settings(db, actor_id=None, changes={"realtime_writes_enabled": True})
    await db.commit()
    started = (await _as(client, person).post("/api/v1/assistant/realtime/session")).json()
    call = {
        "run_id": started["run_id"],
        "name": "leave__request",
        "arguments": {
            "leave_type": "annual",
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
            "reason": None,
        },
    }

    first = (await client.post("/api/v1/assistant/realtime/call", json=call)).json()
    assert first["requires_confirmation"] is True
    assert first["label"]
    assert (await client.get("/api/v1/leave/requests/me")).json() == []

    second = (
        await client.post("/api/v1/assistant/realtime/call", json={**call, "confirmed": True})
    ).json()
    assert second["ok"] is True and second["status"] == 201
    assert len((await client.get("/api/v1/leave/requests/me")).json()) == 1


async def test_a_spoken_conversation_is_recorded(client, boss, realtime_on, wired) -> None:
    started = (await _as(client, boss).post("/api/v1/assistant/realtime/session")).json()
    await client.post(
        "/api/v1/assistant/realtime/call",
        json={"run_id": started["run_id"], "name": "leave__mine", "arguments": {}},
    )
    await client.post(f"/api/v1/assistant/realtime/session/{started['run_id']}/end")

    log = (
        await client.get(f"/api/v1/assistant/admin/runs/{started['run_id']}")
    ).json()
    kinds = [e["kind"] for e in log["events"]]
    assert EventKind.TOOL_CALL in kinds and EventKind.TOOL_RESULT in kinds
    assert log["tool_calls"] == 1
    assert log["status"] == RunStatus.COMPLETED


async def test_somebody_elses_spoken_run_cannot_be_driven(
    client, db, person, realtime_on, wired
) -> None:
    started = (await _as(client, person).post("/api/v1/assistant/realtime/session")).json()
    other = await _make(db, "bilal@hamdaz.com")
    response = await _as(client, other).post(
        "/api/v1/assistant/realtime/call",
        json={"run_id": started["run_id"], "name": "leave__mine", "arguments": {}},
    )
    assert response.status_code == 404


async def test_a_realtime_model_that_does_not_exist_is_refused(client, boss) -> None:
    response = await _as(client, boss).patch(
        "/api/v1/assistant/admin/settings", json={"realtime_model": "gpt-telepathy"}
    )
    assert response.status_code == 400
