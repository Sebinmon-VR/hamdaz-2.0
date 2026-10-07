"""The assistant's loop, answered by Claude.

The loop in ``agent.py`` was written against the OpenAI Responses API: it reads
a stream of ``response.*`` events, keeps its transcript as Responses items
(``function_call``, ``function_call_output``) and sends tools in OpenAI's
function shape. Rewriting it for a second provider would mean two loops that
drift. So this is an adapter: it takes exactly what ``OpenAIChat.stream`` takes,
calls Claude, and yields the same few events the loop reads. Everything the
loop does — permissions, confirmations, client actions, the event log, the
cost — is unchanged.

Three translations, each in one place:

* **Tools.** OpenAI's ``{"type": "function", "parameters": …}`` becomes
  Claude's ``{"name", "description", "input_schema"}``. Tools the catalogue
  defers stay deferred (``defer_loading``) behind Claude's BM25 tool search,
  so the long tail is not read on every turn.
* **The transcript.** Claude's own assistant content (text, thinking,
  tool_use, tool search results) is kept whole as one ``claude_turn`` item —
  Claude needs its thinking and search results back unchanged — and each
  tool_use is *also* reported as a ``function_call`` item, marked ``via:
  claude``, because that is what the loop executes and what its results answer.
  Going back to Claude, the ``claude_turn`` is the assistant message and the
  marked ``function_call`` items are skipped; ``function_call_output`` items
  become one user message of ``tool_result`` blocks.
* **Usage.** Claude's token counts are handed back shaped like a Responses
  usage object, so ``Assistant._account`` prices them unchanged.

A turn the server pauses (``pause_turn``, a long tool search) is resumed here,
inside one call from the loop's point of view.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, Final

import anthropic

from app.assistant.llm import LLMError
from app.core.config import Settings
from app.enquiries.claude import FALLBACK_BETA, explain

logger = logging.getLogger("hamdaz.assistant")

#: Claude's tool search over deferred tools, by keyword.
TOOL_SEARCH: Final = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
#: A paused server turn is resumed at most this many times.
_RESUMES: Final = 3
#: The assistant's effort setting, as Claude's effort levels.
_EFFORT: Final = {"none": "low", "minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh"}


def is_claude(model: str) -> bool:
    return model.startswith("claude-")


def claude_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI function tools as Claude tools, with search when any are deferred."""
    out: list[dict[str, Any]] = []
    deferred = False
    for tool in tools:
        if tool.get("type") != "function":
            continue  # OpenAI's own tool_search entry; Claude's is added below
        entry: dict[str, Any] = {
            "name": tool["name"],
            "description": tool.get("description") or "",
            "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
        }
        if tool.get("defer_loading"):
            entry["defer_loading"] = True
            deferred = True
        out.append(entry)
    if deferred:
        out.append(dict(TOOL_SEARCH))
    return out


def _text_of(content: Any) -> str:
    """A Responses message's content as plain text."""
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts)


def claude_messages(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The loop's history and transcript as Claude messages."""
    messages: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": list(blocks)})

    for item in items:
        kind = item.get("type")
        if kind == "claude_turn":
            push("assistant", [dict(b) for b in item.get("content") or []])
        elif kind == "function_call":
            if item.get("via") == "claude":
                continue  # already inside the claude_turn it came from
            # A call made by another model earlier in the same run: shown as text,
            # since Claude cannot be handed another provider's tool_use.
            push("assistant", [{"type": "text", "text": f"[called {item.get('name')}]"}])
        elif kind == "function_call_output":
            output = item.get("output")
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": item.get("call_id"),
                        "content": output if isinstance(output, str) else json.dumps(output, default=str),
                    }
                ],
            )
        elif kind in (None, "message") and item.get("role") in ("user", "assistant"):
            text = _text_of(item.get("content")).strip()
            if text:
                push(item["role"], [{"type": "text", "text": text}])
        # reasoning items and anything else from another provider are dropped

    # Claude starts with the person speaking.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


def _usage(input_tokens: int, cached: int, output_tokens: int) -> SimpleNamespace:
    """Claude's counts in the Responses usage shape ``_account`` reads."""
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


class ClaudeChat:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def configured(self) -> bool:
        return bool(self._settings.anthropic_api_key)

    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not self._settings.anthropic_api_key:
                raise LLMError("The assistant is set to a Claude model, and no ANTHROPIC_API_KEY is set.")
            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key, timeout=300.0, max_retries=2
            )
        return self._client

    async def stream(
        self,
        *,
        model: str,
        instructions: str,
        input: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str,
        max_output_tokens: int,
        user_key: str,
    ) -> AsyncIterator[Any]:
        """Same arguments as ``OpenAIChat.stream``; yields the events the loop reads."""
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max(1024, min(int(max_output_tokens or 8000), 64_000)),
            "system": instructions,
            "messages": claude_messages(input),
            "output_config": {"effort": _EFFORT.get(reasoning_effort, "medium")},
            "metadata": {"user_id": user_key},
            # The system prompt and the tools are the same turn after turn;
            # caching them is most of what a long conversation costs.
            "cache_control": {"type": "ephemeral"},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        converted = claude_tools(tools)
        if converted:
            params["tools"] = converted
        # Sonnet 5.5 thinks before every answer unless told otherwise, which is
        # seconds of silence before "Good afternoon". At low effort — a chat —
        # it thinks only between tool calls, where thinking earns its keep.
        if model.startswith("claude-sonnet-5-5") and params["output_config"]["effort"] == "low":
            params["thinking"] = {"type": "between_tools"}
        if not params["messages"]:
            raise LLMError("There is nothing to answer.")
        return self._events(params)

    async def _events(self, params: dict[str, Any]) -> AsyncIterator[Any]:
        input_tokens = cached = output_tokens = 0
        status = "completed"
        try:
            for _ in range(_RESUMES + 1):
                async with self.client().beta.messages.stream(**params) as stream:
                    async for event in stream:
                        if event.type == "text":
                            yield SimpleNamespace(type="response.output_text.delta", delta=event.text)
                    message = await stream.get_final_message()

                usage = message.usage
                read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
                written = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
                input_tokens += int(usage.input_tokens or 0) + read + written
                cached += read
                output_tokens += int(usage.output_tokens or 0)

                if message.stop_reason == "refusal":
                    raise LLMError("Claude declined to answer that.")
                content = [block.model_dump(mode="json", exclude_none=True) for block in message.content]
                yield SimpleNamespace(
                    type="response.output_item.done", item={"type": "claude_turn", "content": content}
                )
                for block in message.content:
                    if block.type == "tool_use":
                        yield SimpleNamespace(
                            type="response.output_item.done",
                            item={
                                "type": "function_call",
                                "via": "claude",
                                "call_id": block.id,
                                "name": block.name,
                                "arguments": json.dumps(block.input),
                            },
                        )
                if message.stop_reason == "max_tokens":
                    status = "incomplete"
                if message.stop_reason != "pause_turn":
                    break
                # A paused server turn: hand it back as it is and the server resumes.
                params = {**params, "messages": [*params["messages"], {"role": "assistant", "content": content}]}
        except anthropic.APIError as exc:
            raise LLMError(explain(exc)) from exc
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(status=status, usage=_usage(input_tokens, cached, output_tokens)),
        )


    def _common(self, model: str, effort: str, user_key: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "metadata": {"user_id": user_key},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        # Haiku 4.5 rejects the effort parameter rather than ignoring it.
        if not model.startswith("claude-haiku"):
            params["output_config"] = {"effort": _EFFORT.get(effort, "medium")}
        return params

    async def answer(
        self,
        *,
        model: str,
        instructions: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        reasoning_effort: str = "low",
        max_output_tokens: int = 2000,
        user_key: str,
    ) -> tuple[str, int, int]:
        """``OpenAIChat.answer`` on Claude: one call, JSON when a schema is given."""
        params = self._common(model, reasoning_effort, user_key)
        if schema is not None:
            params["output_config"] = {
                **params.get("output_config", {}),
                "format": {"type": "json_schema", "schema": schema},
            }
        try:
            message = await self.client().beta.messages.create(
                **params,
                max_tokens=max(1024, max_output_tokens),
                system=instructions,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            raise LLMError(explain(exc)) from exc
        if message.stop_reason == "refusal":
            raise LLMError("Claude declined to answer that.")
        text = next((b.text for b in message.content if b.type == "text"), "")
        return text.strip(), int(message.usage.input_tokens or 0), int(message.usage.output_tokens or 0)

    async def research(
        self,
        *,
        model: str,
        instructions: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        web_search: bool = True,
        reasoning_effort: str = "medium",
        max_output_tokens: int = 4000,
        user_key: str,
    ) -> tuple[str, int, int]:
        """``OpenAIChat.research`` on Claude: web search, then text or JSON.

        JSON comes back through a ``record_findings`` tool rather than
        structured outputs, so search citations and the schema never meet.
        """
        tools: list[dict[str, Any]] = []
        if web_search:
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 10})
        system = instructions
        if schema is not None:
            tools.append(
                {
                    "name": "record_findings",
                    "description": "Record the answer. Call it once, at the end.",
                    "input_schema": schema,
                }
            )
            system += "\n\nWhen you have the answer, call record_findings once with it."
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tokens_in = tokens_out = 0
        for _ in range(_RESUMES + 1):
            try:
                message = await self.client().beta.messages.create(
                    **self._common(model, reasoning_effort, user_key),
                    max_tokens=max(2000, max_output_tokens),
                    system=system,
                    messages=messages,
                    tools=tools or anthropic.omit,
                )
            except anthropic.APIError as exc:
                raise LLMError(explain(exc)) from exc
            tokens_in += int(message.usage.input_tokens or 0)
            tokens_out += int(message.usage.output_tokens or 0)
            if message.stop_reason == "refusal":
                raise LLMError("Claude declined to answer that.")
            for block in message.content:
                if block.type == "tool_use" and block.name == "record_findings":
                    return json.dumps(block.input), tokens_in, tokens_out
            messages.append({"role": "assistant", "content": message.content})
            if message.stop_reason == "pause_turn":
                continue
            if schema is None:
                text = "".join(b.text for b in message.content if b.type == "text")
                return text.strip(), tokens_in, tokens_out
            messages.append({"role": "user", "content": "Now record the answer with record_findings."})
        raise LLMError("The lookup did not finish.")


class ModelRouter:
    """One object the app holds as "the model": Claude for ``claude-*`` keys, OpenAI otherwise.

    The loop, the report briefer and the workflow steps call ``stream`` and
    ``answer``/``research`` on it as before. Speech and the realtime voice
    session are OpenAI features and stay with OpenAI.
    """

    def __init__(self, openai_chat: Any, claude: ClaudeChat) -> None:
        self.openai = openai_chat
        self.claude = claude

    @property
    def configured(self) -> bool:
        return self.openai.configured or self.claude.configured

    def configured_for(self, model: str) -> bool:
        return self.claude.configured if is_claude(model) else self.openai.configured

    async def stream(self, **kwargs: Any) -> AsyncIterator[Any]:
        if is_claude(kwargs.get("model", "")):
            return await self.claude.stream(**kwargs)
        return await self.openai.stream(**kwargs)

    async def answer(self, **kwargs: Any) -> tuple[str, int, int]:
        if is_claude(kwargs.get("model", "")):
            return await self.claude.answer(**kwargs)
        return await self.openai.answer(**kwargs)

    async def research(self, **kwargs: Any) -> tuple[str, int, int]:
        if is_claude(kwargs.get("model", "")):
            return await self.claude.research(**kwargs)
        return await self.openai.research(**kwargs)

    def __getattr__(self, name: str) -> Any:
        # read_documents, speak, realtime_secret, client: OpenAI's, unchanged.
        return getattr(self.openai, name)
