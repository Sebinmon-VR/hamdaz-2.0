"""The model an agent leans on — last, and only where the system cannot answer.

Every agent step tries the app first: the task mirror, our supplier quotes and
quote requests, the document reader's tables, templates. A step reaches this
file only for what is genuinely judgement or prose — reading a scanned tender,
an instruction the owner wrote in their own words, a web search they switched
on — and then it asks Claude Haiku 5.5 at low effort (``AGENT_MODEL``,
``AGENT_EFFORT``), because the user asked for low cost.

Two calls, both answering JSON held to a schema, so nothing comes back as
loose prose to be parsed:

* ``ask`` — structured outputs over text and documents.
* ``search`` — the web search server tool, with the findings returned through
  a strict custom tool so search citations and a schema never meet in one
  answer (the same arrangement as the enquiry research).

Haiku has no server-side fallback, so none is requested: a refusal is raised
as an ``LLMError`` and the step says so.

Shares the enquiry module's error wording and price table rather than keeping
a second copy of either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import anthropic

from app.core.config import Settings
from app.enquiries.claude import LLMError, _tokens, cost_of, explain

#: Haiku 5.5 is not on the dynamic-filtering web search; this is the basic one.
WEB_SEARCH_TOOL: Final = "web_search_20250305"
#: USD per web search, on top of tokens.
SEARCH_FEE: Final = Decimal("0.01")
_SEARCH_ROUNDS: Final = 4


@dataclass(slots=True)
class Spent:
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    #: The free model that answered instead, e.g. "groq:openai/gpt-oss-120b".
    free_by: str | None = None

    def cost(self, model: str) -> Decimal:
        return cost_of(model, self.input_tokens, self.output_tokens) + SEARCH_FEE * self.searches


def _refused(message: Any) -> None:
    if getattr(message, "stop_reason", None) == "refusal":
        details = getattr(message, "stop_details", None)
        why = getattr(details, "explanation", None) or "no reason given"
        raise LLMError(f"The model declined this step ({why}).")


def _searches(message: Any) -> int:
    usage = getattr(message, "usage", None)
    tool_use = getattr(usage, "server_tool_use", None)
    return int(getattr(tool_use, "web_search_requests", 0) or 0)


class AgentModel:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def configured(self) -> bool:
        return bool(self._settings.anthropic_api_key)

    @property
    def model(self) -> str:
        return self._settings.agent_model.strip() or "claude-haiku-5-5"

    @property
    def effort(self) -> str:
        return self._settings.agent_effort.strip() or "low"

    @property
    def web_allowed(self) -> bool:
        return self._settings.agent_web_search

    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not self._settings.anthropic_api_key:
                raise LLMError("This step needs the model, and no Anthropic API key is set (ANTHROPIC_API_KEY).")
            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key, timeout=300.0, max_retries=2
            )
        return self._client

    async def ask(
        self,
        *,
        system: str,
        content: list[dict[str, Any]],
        schema: dict[str, Any],
        user_key: str,
        spent: Spent,
        max_tokens: int = 16_000,
    ) -> dict[str, Any]:
        """One answer, as JSON shaped by ``schema``."""
        try:
            async with self.client().messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                metadata={"user_id": user_key},
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise LLMError(explain(exc)) from exc
        used_in, used_out = _tokens(message)
        spent.input_tokens += used_in
        spent.output_tokens += used_out
        _refused(message)
        if message.stop_reason == "max_tokens":
            raise LLMError("The answer was longer than one reply can hold.")
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise LLMError("The model did not answer in the expected shape.") from exc
        if not isinstance(payload, dict):
            raise LLMError("The model did not answer in the expected shape.")
        return payload

    async def search(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        user_key: str,
        spent: Spent,
    ) -> dict[str, Any]:
        """Search the web, then record the findings as JSON shaped by ``schema``."""
        tools: list[dict[str, Any]] = [
            {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": self._settings.agent_web_max_uses},
            {
                "name": "record_findings",
                "description": "Record what the search found. Call it once, at the end.",
                "strict": True,
                "input_schema": schema,
            },
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        system = f"{system}\n\nWhen you have searched, call record_findings once."
        for _ in range(_SEARCH_ROUNDS):
            try:
                message = await self.client().messages.create(
                    model=self.model,
                    max_tokens=16_000,
                    system=system,
                    messages=messages,
                    tools=tools,
                    output_config={"effort": self.effort},
                    metadata={"user_id": user_key},
                )
            except anthropic.APIError as exc:
                raise LLMError(explain(exc)) from exc
            used_in, used_out = _tokens(message)
            spent.input_tokens += used_in
            spent.output_tokens += used_out
            spent.searches += _searches(message)
            _refused(message)
            for block in message.content:
                if block.type == "tool_use" and block.name == "record_findings":
                    return dict(block.input)
            messages.append({"role": "assistant", "content": message.content})
            if message.stop_reason != "pause_turn":
                # A paused search resumes on the turn as it is; anything else is nudged.
                messages.append({"role": "user", "content": "Now record your findings with the record_findings tool."})
        raise LLMError("The web search did not return its findings.")
