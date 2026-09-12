"""The assistant on the screen, over HTTP: pressing, filling, and who may delete.

Two things are tested here that the pure tests cannot reach. First, the
park-and-resume of a *client* tool — the turn stops as ``awaiting_client``,
the browser reports, the turn carries on with the report as a tool result —
which is the same mechanism as a confirmation with a different party
answering. Second, that the delete rule is visible at the edges: ``status``
says whether this person may delete, and the tool list a member is shown
carries no delete however the policy rows are set.

The model is the same stub as in ``test_assistant_routes``; its fixtures and
helpers are imported rather than copied.
"""

from __future__ import annotations

import pytest

from app.assistant.cache import PlacesCache
from app.assistant.catalogue import TOOLS_BY_KEY
from app.models.assistant import EventKind, RunStatus
from tests.test_assistant_routes import (  # noqa: F401 - fixtures by import
    _as,
    _chat,
    boss,
    calls,
    llm,
    manager,
    only,
    person,
    says,
    setup,
    sse,
    text_of,
    wired,
)

@pytest.fixture(autouse=True)
def places(client) -> None:
    """The places cache, which the conftest app does not carry.

    ``_where`` and ``app.open`` resolve screens through it; a message that
    says where the person is reaches it on every turn.
    """
    app = client._transport.app
    if not hasattr(app.state, "assistant_places"):
        app.state.assistant_places = PlacesCache()


SCREEN = [
    {"kind": "heading", "label": "New quote", "value": "h1"},
    {"kind": "field", "label": "Title", "value": None},
    {"kind": "button", "label": "Save", "value": None},
    {"kind": "button", "label": "Delete", "value": None},
]


async def _send(client, conversation_id: str, text: str, **extra):
    return await client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        json={"text": text, **extra},
    )


# ── who may delete ─────────────────────────────────────────────────────


async def test_status_says_whether_this_person_may_delete(client, person, manager) -> None:
    mine = (await _as(client, person).get("/api/v1/assistant/status")).json()
    assert mine["admitted"] is True and mine["can_delete"] is False

    theirs = (await _as(client, manager).get("/api/v1/assistant/status")).json()
    assert theirs["can_delete"] is True


async def test_a_member_is_shown_no_delete_tool_at_all(client, person) -> None:
    status = (await _as(client, person).get("/api/v1/assistant/status")).json()
    offered = {t["key"] for m in status["modules"] for t in m["tools"]}
    destructive = {k for k in offered if TOOLS_BY_KEY[k].is_destructive}
    assert destructive == set()
    # The screen tools are there for everybody.
    assert {"app.click", "app.fill", "app.scroll", "app.screen"} <= offered


async def test_a_manager_is_shown_the_deletes(client, manager) -> None:
    # ``roles`` is gated on holding an admin role rather than on a module
    # grant, which the test fixtures do not seed — so it is the one module
    # whose deletes turn purely on the rule under test.
    status = (await _as(client, manager).get("/api/v1/assistant/status")).json()
    offered = {t["key"] for m in status["modules"] for t in m["tools"]}
    assert {"roles.delete", "roles.revoke"} <= offered
    assert all(TOOLS_BY_KEY[k].is_destructive for k in ("roles.delete", "roles.revoke"))


# ── the screen reaches the model ───────────────────────────────────────


async def test_the_controls_on_the_screen_reach_the_model(client, person, wired) -> None:
    wired.plays(says("You are on the new quote form."))
    conversation_id = await _chat(client, person)
    response = await _send(
        client, conversation_id, "what can I do here", page="/quote-requests/new", screen=SCREEN
    )
    assert response.status_code == 200
    instructions = wired.calls[0]["instructions"]
    assert "Controls on this screen" in instructions
    assert "button: Save" in instructions
    assert "field: Title" in instructions
    assert "heading: New quote" in instructions


async def test_a_screen_tool_is_offered_to_the_model(client, person, wired) -> None:
    wired.plays(says("Hello."))
    conversation_id = await _chat(client, person)
    await _send(client, conversation_id, "hi")
    names = {t.get("name") for t in wired.calls[0]["tools"]}
    assert {"app__click", "app__fill", "app__scroll", "app__screen"} <= names


# ── park on the browser, resume with its report ────────────────────────


async def test_a_press_parks_the_turn_on_the_browser(client, person, wired) -> None:
    wired.plays(calls("app__click", {"label": "Save", "nth": None}))
    conversation_id = await _chat(client, person)
    events = sse(
        (await _send(client, conversation_id, "press save", page="/x", screen=SCREEN)).text
    )

    handed = only(events, "client_action")[0]
    assert [a["tool_key"] for a in handed["actions"]] == ["app.click"]
    assert handed["actions"][0]["arguments"]["label"] == "Save"
    assert handed["actions"][0]["kind"] == "client"
    assert only(events, "done")[0]["status"] == RunStatus.AWAITING_CLIENT
    # Nothing ran on this side: there is no route behind a press.
    assert only(events, "tool_result") == []
    assert only(events, "confirm") == []

    kept = (await client.get(f"/api/v1/assistant/conversations/{conversation_id}")).json()
    assert kept["pending"]["status"] == RunStatus.AWAITING_CLIENT
    assert kept["pending"]["actions"][0]["tool_key"] == "app.click"


async def test_the_browsers_report_resumes_the_turn(client, person, wired) -> None:
    wired.plays(
        calls("app__click", {"label": "Save", "nth": None}, call_id="c1"),
        says("Pressed Save for you."),
    )
    conversation_id = await _chat(client, person)
    first = sse((await _send(client, conversation_id, "press save", page="/x", screen=SCREEN)).text)
    run_id = only(first, "client_action")[0]["run_id"]

    response = await client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/client-result",
        json={
            "run_id": run_id,
            "results": [{"call_id": "c1", "ok": True, "output": '{"pressed": "button: Save"}'}],
        },
    )
    assert response.status_code == 200, response.text
    events = sse(response.text)
    result = only(events, "tool_result")[0]
    assert result["tool_key"] == "app.click" and result["ok"] is True
    assert text_of(events) == "Pressed Save for you."
    assert only(events, "done")[0]["status"] == RunStatus.COMPLETED

    # The model was handed the report as the output of its own call.
    resumed = wired.calls[1]["input"]
    outputs = [i for i in resumed if i.get("type") == "function_call_output"]
    assert outputs[-1]["call_id"] == "c1"
    assert "pressed" in outputs[-1]["output"]

    # And the chat is free again.
    kept = (await client.get(f"/api/v1/assistant/conversations/{conversation_id}")).json()
    assert kept["pending"] is None


async def test_an_action_the_browser_says_nothing_about_is_not_done(
    client, person, wired
) -> None:
    wired.plays(
        calls("app__scroll", {"to": "bottom"}, call_id="c9"),
        says("I could not scroll."),
    )
    conversation_id = await _chat(client, person)
    first = sse((await _send(client, conversation_id, "scroll down", page="/x")).text)
    run_id = only(first, "client_action")[0]["run_id"]

    events = sse(
        (
            await client.post(
                f"/api/v1/assistant/conversations/{conversation_id}/client-result",
                json={"run_id": run_id, "results": []},
            )
        ).text
    )
    assert only(events, "tool_result")[0]["ok"] is False
    outputs = [i for i in wired.calls[1]["input"] if i.get("type") == "function_call_output"]
    assert "did not report" in outputs[-1]["output"]


async def test_a_second_message_waits_for_the_screen(client, person, wired) -> None:
    wired.plays(calls("app__screen", {}))
    conversation_id = await _chat(client, person)
    await _send(client, conversation_id, "what is here", page="/x")
    refused = await _send(client, conversation_id, "and now?")
    assert refused.status_code == 409
    assert "screen" in refused.json()["detail"]


async def test_reporting_on_a_run_that_is_not_waiting_is_refused(client, person, wired) -> None:
    wired.plays(says("Plain answer."))
    conversation_id = await _chat(client, person)
    first = sse((await _send(client, conversation_id, "hello")).text)
    run_id = only(first, "run")[0]["run_id"]
    response = await client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/client-result",
        json={"run_id": run_id, "results": []},
    )
    assert response.status_code == 409


async def test_the_run_log_records_the_handoff(client, boss, wired) -> None:
    wired.plays(
        calls("app__fill", {"label": "Title", "value": "Pumps", "nth": None}, call_id="f1"),
        says("Filled it in."),
    )
    conversation_id = await _chat(client, boss)
    first = sse((await _send(client, conversation_id, "fill title", page="/x", screen=SCREEN)).text)
    run_id = only(first, "client_action")[0]["run_id"]
    await client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/client-result",
        json={"run_id": run_id, "results": [{"call_id": "f1", "ok": True, "output": "{}"}]},
    )
    detail = (await client.get(f"/api/v1/assistant/admin/runs/{run_id}")).json()
    kinds = [e["kind"] for e in detail["events"]]
    assert EventKind.CLIENT_ACTION_REQUESTED in kinds
    assert EventKind.CLIENT_ACTION_RESULT in kinds
    assert detail["status"] == RunStatus.COMPLETED
