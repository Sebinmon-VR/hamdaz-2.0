"""The enquiry analysis's model: Claude, through the Anthropic API.

Two calls, both from ``service.Run``:

* ``read_documents`` — every requirement document in one request, answered
  as JSON held to ``reading.SCHEMA`` by structured outputs. PDFs and scans go
  as document blocks, which Claude reads page by page, images included, so a
  scanned tender needs no OCR here. Streamed, because a tender's item list is
  long and a long non-streamed answer can outrun the HTTP timeout.
* ``research`` — a few new items at a time, with the web search server tool.
  The findings come back through a strict custom tool (``record_findings``)
  rather than structured outputs, so the search results' citations and the
  schema never meet in one answer. A search that runs long pauses
  (``pause_turn``) and is resumed by sending the turn back as it is.

Both opt into server-side fallbacks: a request Claude's safeguards decline is
re-run on Anthropic's recommended model in the same call, instead of failing
the analysis.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Final

import anthropic

from app.assistant.llm import LLMError
from app.core.config import Settings

logger = logging.getLogger("hamdaz.enquiries")

FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"

#: USD per million tokens (input, output), first-party API rates, September 2026.
PRICES: Final[dict[str, tuple[Decimal, Decimal]]] = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}

#: How many times one research request may be resumed or nudged.
_RESEARCH_ROUNDS: Final = 4


def explain(exc: Exception) -> str:
    """An Anthropic error in words a person can act on."""
    text = str(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return "Anthropic rejected the API key. Create a new key at console.anthropic.com and set ANTHROPIC_API_KEY."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "The Anthropic key is not allowed to use this model."
    if "credit balance" in text.lower():
        return "The Anthropic account has no credit left. Add credit at console.anthropic.com, under Billing."
    if isinstance(exc, anthropic.RateLimitError):
        return "Anthropic is rate limiting this key. Try again in a few minutes."
    if isinstance(exc, anthropic.NotFoundError):
        return "Anthropic does not know the configured model (ENQUIRY_MODEL)."
    if isinstance(exc, anthropic.BadRequestError):
        return f"Anthropic rejected the request: {getattr(exc, 'message', text)}"
    if isinstance(exc, anthropic.APITimeoutError):
        return "Anthropic took too long to answer. Try again."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach Anthropic. Try again shortly."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic returned an error ({exc.status_code}). Try again shortly."
    return f"The model call failed: {exc}"


def cost_of(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Token cost in USD. Web searches carry a per-search fee on top, not counted here."""
    price_in, price_out = PRICES.get(model, (Decimal(0), Decimal(0)))
    total = (Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out) / Decimal(1_000_000)
    return total.quantize(Decimal("0.0001"))


def _tokens(message: Any) -> tuple[int, int]:
    usage = getattr(message, "usage", None)
    if usage is None:
        return 0, 0
    read = (
        int(getattr(usage, "input_tokens", 0) or 0)
        + int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        + int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    )
    return read, int(getattr(usage, "output_tokens", 0) or 0)


def _refused(message: Any) -> None:
    if getattr(message, "stop_reason", None) == "refusal":
        details = getattr(message, "stop_details", None)
        why = getattr(details, "explanation", None) or "no reason given"
        raise LLMError(f"Claude declined to read these documents ({why}).")


class ClaudeEnquiry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def configured(self) -> bool:
        return bool(self._settings.anthropic_api_key)

    @property
    def model(self) -> str:
        return self._settings.enquiry_model.strip() or "claude-opus-5-5"

    @property
    def effort(self) -> str:
        return self._settings.enquiry_effort.strip() or "medium"

    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not self._settings.anthropic_api_key:
                raise LLMError("The enquiry analysis needs an Anthropic API key (ANTHROPIC_API_KEY).")
            # Ten minutes: a forty-page tender is a long read. Retries cover
            # 429 and 5xx with backoff.
            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key, timeout=600.0, max_retries=2
            )
        return self._client

    async def read_documents(
        self, *, instructions: str, content: list[dict[str, Any]], schema: dict[str, Any], user_key: str
    ) -> tuple[str, int, int]:
        """The requirement documents, read into JSON shaped by ``schema``."""
        try:
            async with self.client().beta.messages.stream(
                model=self.model,
                max_tokens=64_000,
                system=instructions,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                metadata={"user_id": user_key},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise LLMError(explain(exc)) from exc
        _refused(message)
        if message.stop_reason == "max_tokens":
            raise LLMError("The item list was longer than one answer can hold. Split the documents and run again.")
        text = next((b.text for b in message.content if b.type == "text"), "")
        tokens_in, tokens_out = _tokens(message)
        return text.strip(), tokens_in, tokens_out

    async def research(
        self, *, instructions: str, prompt: str, schema: dict[str, Any], user_key: str
    ) -> tuple[str, int, int]:
        """Web search for a few items; the findings as JSON shaped by ``schema``."""
        tools: list[dict[str, Any]] = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 10},
            {
                "name": "record_findings",
                "description": "Record what the search found for every numbered item. Call it once, at the end.",
                "strict": True,
                "input_schema": schema,
            },
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        system = f"{instructions}\n\nWhen you have searched, call record_findings once with every item."
        tokens_in = tokens_out = 0
        for _ in range(_RESEARCH_ROUNDS):
            try:
                message = await self.client().beta.messages.create(
                    model=self.model,
                    max_tokens=16_000,
                    system=system,
                    messages=messages,
                    tools=tools,
                    output_config={"effort": self.effort},
                    metadata={"user_id": user_key},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                )
            except anthropic.APIError as exc:
                raise LLMError(explain(exc)) from exc
            used_in, used_out = _tokens(message)
            tokens_in, tokens_out = tokens_in + used_in, tokens_out + used_out
            _refused(message)
            for block in message.content:
                if block.type == "tool_use" and block.name == "record_findings":
                    return json.dumps(block.input), tokens_in, tokens_out
            if message.stop_reason == "pause_turn":
                # The server stopped mid-search; sending the turn back resumes it.
                messages.append({"role": "assistant", "content": message.content})
                continue
            messages.append({"role": "assistant", "content": message.content})
            messages.append(
                {"role": "user", "content": "Now record your findings with the record_findings tool."}
            )
        raise LLMError("The web lookup did not return its findings.")
