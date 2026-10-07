"""The assistant on Claude: the translations, without a network."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.assistant.catalogue import LIVE_TOOLS, MODELS_BY_KEY
from app.assistant.claude_chat import (
    TOOL_SEARCH,
    ClaudeChat,
    ModelRouter,
    claude_messages,
    claude_tools,
    is_claude,
)
from app.core.config import get_settings


def test_tools_keep_their_schema_and_deferral() -> None:
    definitions = [t.definition() for t in LIVE_TOOLS]
    converted = claude_tools([*definitions, {"type": "tool_search"}])
    by_name = {t["name"]: t for t in converted}
    assert len(by_name) == len(LIVE_TOOLS) + 1  # every tool, plus Claude's own search
    assert by_name[TOOL_SEARCH["name"]] == TOOL_SEARCH
    for definition in definitions:
        tool = by_name[definition["name"]]
        assert tool["input_schema"] == definition["parameters"]
        assert tool.get("defer_loading", False) == definition.get("defer_loading", False)
        assert "strict" not in tool and "type" not in tool


def test_no_search_tool_when_nothing_is_deferred() -> None:
    loaded = [{**t.definition(), "defer_loading": False} for t in LIVE_TOOLS[:3]]
    assert all(t["name"] != TOOL_SEARCH["name"] for t in claude_tools(loaded))


def test_a_transcript_round_trips_into_claude_messages() -> None:
    turn = {
        "type": "claude_turn",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "leave__summary", "input": {}},
        ],
    }
    items = [
        {"role": "assistant", "content": "An earlier answer, before this person spoke."},
        {"role": "user", "content": "How much leave do I have?"},
        turn,
        {"type": "function_call", "via": "claude", "call_id": "toolu_1", "name": "leave__summary", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "toolu_1", "output": json.dumps({"remaining": 16})},
        {"type": "reasoning", "summary": []},
    ]
    messages = claude_messages(items)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == [{"type": "text", "text": "How much leave do I have?"}]
    # Claude's own turn comes back whole — thinking included — and the marked
    # function_call is not sent a second time.
    assert messages[1]["content"] == turn["content"]
    assert messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": json.dumps({"remaining": 16})}
    ]


def test_several_results_are_one_user_message() -> None:
    items = [
        {"role": "user", "content": "Two things."},
        {"type": "claude_turn", "content": [
            {"type": "tool_use", "id": "a", "name": "x", "input": {}},
            {"type": "tool_use", "id": "b", "name": "y", "input": {}},
        ]},
        {"type": "function_call_output", "call_id": "a", "output": "1"},
        {"type": "function_call_output", "call_id": "b", "output": "2"},
    ]
    last = claude_messages(items)[-1]
    assert last["role"] == "user" and [b["tool_use_id"] for b in last["content"]] == ["a", "b"]


def test_openai_history_reads_as_text() -> None:
    items = [
        {"role": "user", "content": "Hi"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello."}]},
        {"type": "function_call", "call_id": "c", "name": "old_tool", "arguments": "{}"},
    ]
    messages = claude_messages(items)
    assert messages[1]["content"][0] == {"type": "text", "text": "Hello."}
    assert messages[1]["content"][1]["text"] == "[called old_tool]"


def test_models_and_routing() -> None:
    for key in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"):
        assert key in MODELS_BY_KEY and is_claude(key)
    assert not is_claude("gpt-5.6-terra")
    openai_side = SimpleNamespace(configured=False, speak="openai-speak")
    settings = get_settings().model_copy()
    settings.anthropic_api_key = "test"
    router = ModelRouter(openai_side, ClaudeChat(settings))
    assert router.configured_for("claude-opus-5-5") and not router.configured_for("gpt-5.6-terra")
    assert router.speak == "openai-speak"  # speech stays OpenAI's


async def test_stream_yields_the_loops_events() -> None:
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=50, cache_creation_input_tokens=0)
    tool = SimpleNamespace(type="tool_use", id="toolu_9", name="app__open", input={"page": "leave"},
                           model_dump=lambda **kw: {"type": "tool_use", "id": "toolu_9", "name": "app__open", "input": {"page": "leave"}})
    text = SimpleNamespace(type="text", text="Opening it.", model_dump=lambda **kw: {"type": "text", "text": "Opening it."})
    final = SimpleNamespace(usage=usage, stop_reason="tool_use", content=[text, tool])

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            async def gen():
                yield SimpleNamespace(type="text", text="Opening it.")
            return gen()

        async def get_final_message(self):
            return final

    sent: dict = {}

    def stream(**kw):
        sent.update(kw)
        return FakeStream()

    settings = get_settings().model_copy()
    settings.anthropic_api_key = "test"
    chat = ClaudeChat(settings)
    chat._client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    events = await chat.stream(
        model="claude-opus-5-5", instructions="be brief", input=[{"role": "user", "content": "leave"}],
        tools=[LIVE_TOOLS[0].definition()], reasoning_effort="minimal", max_output_tokens=500, user_key="u",
    )
    got = [e async for e in events]
    assert [e.type for e in got] == [
        "response.output_text.delta", "response.output_item.done", "response.output_item.done", "response.completed",
    ]
    call = got[2].item
    assert call == {"type": "function_call", "via": "claude", "call_id": "toolu_9", "name": "app__open",
                    "arguments": json.dumps({"page": "leave"})}
    assert got[3].response.usage.input_tokens == 150
    assert got[3].response.usage.input_tokens_details.cached_tokens == 50
    assert sent["output_config"] == {"effort": "low"} and sent["max_tokens"] == 1024
    assert sent["fallbacks"] == "default" and sent["cache_control"] == {"type": "ephemeral"}
