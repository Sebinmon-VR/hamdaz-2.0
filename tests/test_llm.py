"""The optional text model: providers in order, fallback on a rate limit, and
nothing trusted that does not parse.

Every provider is an ``httpx.MockTransport``. No key here is real and no
request leaves the process.
"""

from __future__ import annotations

import json

import httpx

from app.core.config import get_settings
from app.core.llm import TextModel, parse_json_object, providers_from, trim


def settings_with(**overrides):
    settings = get_settings().model_copy()
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def model_with(handler, **overrides) -> TextModel:
    settings = settings_with(
        llm_providers="groq,cerebras,openrouter",
        groq_api_key="g", cerebras_api_key="c", openrouter_api_key="o",
        anthropic_api_key="", **overrides,
    )
    return TextModel(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def chat_reply(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})


# ── which providers ────────────────────────────────────────────────────


def test_only_providers_with_a_key_are_used_in_the_order_named() -> None:
    settings = settings_with(
        llm_providers="openrouter, groq ,cerebras,ollama,anthropic",
        groq_api_key="g", cerebras_api_key="", openrouter_api_key="o",
        ollama_base_url="", anthropic_api_key="",
    )
    assert [p.name for p in providers_from(settings)] == ["openrouter", "groq"]


def test_no_keys_means_no_model_and_no_call() -> None:
    settings = settings_with(
        llm_providers="groq,cerebras,openrouter", groq_api_key="", cerebras_api_key="",
        openrouter_api_key="", anthropic_api_key="",
    )
    assert providers_from(settings) == []
    assert settings.text_model_configured is False


def test_ollama_needs_no_key_only_an_address() -> None:
    settings = settings_with(llm_providers="ollama", ollama_base_url="http://box:11434/v1")
    names = [p.name for p in providers_from(settings)]
    assert names == ["ollama"]
    assert providers_from(settings)[0].base_url == "http://box:11434/v1"


# ── asking ─────────────────────────────────────────────────────────────


async def test_the_first_provider_that_answers_wins() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return chat_reply(json.dumps({"reference_number": "6000150626"}))

    model = model_with(handler)
    answer = await model.extract(
        instructions="read", text="RFQ 6000150626", shape={"reference_number": ""}
    )
    assert answer == ({"reference_number": "6000150626"}, "groq:openai/gpt-oss-120b")
    assert seen == ["api.groq.com"]


async def test_a_rate_limit_moves_to_the_next_provider() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "api.groq.com":
            return httpx.Response(429, json={"error": "rate limited"})
        return chat_reply('{"reference_number": "X"}')

    model = model_with(handler)
    answer = await model.extract(instructions="read", text="x", shape={"reference_number": ""})
    assert answer is not None and answer[1].startswith("cerebras:")
    assert seen == ["api.groq.com", "api.cerebras.ai"]


async def test_every_provider_refusing_is_no_answer_not_an_error() -> None:
    model = model_with(lambda r: httpx.Response(503, json={}))
    assert await model.extract(instructions="read", text="x", shape={"a": ""}) is None


async def test_prose_around_the_json_is_tolerated() -> None:
    model = model_with(lambda r: chat_reply('Sure! ```json\n{"a": 1}\n```'))
    answer = await model.extract(instructions="i", text="t", shape={"a": 0})
    assert answer == ({"a": 1}, "groq:openai/gpt-oss-120b")


async def test_a_reply_with_no_json_is_no_answer_from_that_provider() -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.host)
        return chat_reply("no json here")

    model = model_with(handler)
    assert await model.extract(instructions="i", text="t", shape={"a": 0}) is None
    # Every provider was given the chance, and none was believed.
    assert asked == ["api.groq.com", "api.cerebras.ai", "openrouter.ai"]


async def test_the_request_asks_for_json_and_sends_the_document() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return chat_reply("{}")

    model = model_with(handler)
    await model.extract(instructions="Read it", text="THE DOCUMENT", shape={"k": "v"})
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["temperature"] == 0
    assert "THE DOCUMENT" in captured["messages"][1]["content"]
    assert '"k": "v"' in captured["messages"][1]["content"]


async def test_anthropic_speaks_its_own_protocol() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-api-key")
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"a": 2}'}]})

    settings = settings_with(llm_providers="anthropic", anthropic_api_key="sk-test")
    model = TextModel(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    answer = await model.extract(instructions="i", text="t", shape={"a": 0})
    assert answer == ({"a": 2}, f"anthropic:{settings.extract_model}")
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["key"] == "sk-test"
    assert "system" in captured and captured["messages"][0]["role"] == "user"


# ── the small parts ────────────────────────────────────────────────────


def test_a_long_document_keeps_its_head_and_tail() -> None:
    text = "H" * 700 + "M" * 1000 + "T" * 300
    cut = trim(text, 1000)
    assert cut.startswith("H" * 700) and cut.endswith("T" * 300)
    assert "left out" in cut and "M" * 10 not in cut
    assert trim("short", 1000) == "short"


def test_parse_json_object_takes_the_first_object_only() -> None:
    assert parse_json_object('{"a": 1} trailing') == {"a": 1}
    assert parse_json_object("[1, 2]") is None
    assert parse_json_object("") is None
