"""A text model, when one is wanted, behind whichever free endpoint is up.

Deterministic readers do the work in this application. This is the second
pass they hand a document to when the words and tables did not settle it: a
supplier quotation laid out in a way the table reader cannot follow, a tender
whose requirements are paragraphs rather than rows. It is optional, it is
text only — save that Claude, when configured, is shown the file itself where
there is no text to read (:meth:`TextModel.see`) — and everything it answers
goes through the same validation the parsers' answers do.

**Providers, in order, with fallback.** The free tiers ration by requests per
day and tokens per minute, and a 429 from one is not a reason to fail an
upload. So the configured providers are tried in order — Groq, then Cerebras,
then OpenRouter, say — and the first that answers wins. All but one speak the
OpenAI chat-completions protocol, so they are one client with different base
URLs; Anthropic has its own shape and its own adapter, kept for as long as
there is credit on the key.

**JSON, validated here.** Every call asks for a JSON object in a stated shape
and parses what comes back defensively. A provider's own JSON mode is
requested where it exists, but never relied on: the text is cut down to the
first ``{ … }`` block and parsed, and anything that fails to parse is treated
as no answer. Nothing a model returns is trusted to add up — figures are
transcribed by it and computed by us.

**Text is trimmed to fit.** The free tiers cap tokens per minute at a few
thousand. Documents longer than the configured budget are sent as their head
and tail, which is where the labelled fields and the totals live; the middle
of a forty-page tender is boilerplate this is not asked about.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Final

import httpx

from app.core.config import Settings

logger = logging.getLogger("hamdaz.llm")

#: Where each provider lives. Ollama's is configured, since it is a machine of
#: ours; Anthropic is handled apart, its protocol being its own.
_BASE_URLS: Final = {
    "groq": "https://api.groq.com/openai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
_ANTHROPIC_URL: Final = "https://api.anthropic.com/v1/messages"

#: Status codes that mean "try the next provider" rather than "give up".
_TRY_NEXT: Final = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class ModelError(Exception):
    """A provider refused or could not be reached. Never shown as a failure of
    the upload it was part of; the caller carries on without the answer."""


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True, slots=True)
class Answer:
    """A JSON object from the first provider that gave a usable one."""

    payload: dict[str, Any]
    #: "groq:openai/gpt-oss-120b" — who answered, for the audit trail.
    who: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def paid(self) -> bool:
        """Answered by Claude, which is billed; the free tiers are not."""
        return self.who.startswith("anthropic:")


# ── what each call cost ──────────────────────────────────────────────────

#: Claude's list prices, USD per million tokens (input, output). The free
#: tiers — Groq, Cerebras, OpenRouter's free models, a machine of ours running
#: Ollama — cost nothing and are not listed. A Claude model missing here is
#: priced at Opus's rate rather than as free, so an unknown model is never
#: reported as costing nothing.
CLAUDE_PRICES: Final[dict[str, tuple[Decimal, Decimal]]] = {
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
}
_UNKNOWN_CLAUDE: Final = (Decimal("5"), Decimal("25"))


def cost_of(provider: str, model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """What one call cost in USD: Claude at its list price, the free tiers nothing."""
    if provider != "anthropic":
        return Decimal(0)
    price_in, price_out = CLAUDE_PRICES.get(model, _UNKNOWN_CLAUDE)
    return (Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out) / Decimal(
        1_000_000
    )


@dataclass(slots=True)
class ModelCall:
    """One answer a provider gave — billed whether or not it was usable."""

    provider: str
    model: str
    #: What was being read, for the person looking at the bill.
    label: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    #: False when the answer was unparseable or failed the caller's check, and
    #: the next provider was asked: paid for, and thrown away.
    used: bool = False


_CALLS: ContextVar[list[ModelCall] | None] = ContextVar("llm_calls", default=None)


@contextmanager
def capture() -> Iterator[list[ModelCall]]:
    """Collect every model call made inside the block, so the caller can file
    them against what they were for — a quote, say. Calls made outside any
    block are not collected; nothing is lost by them, they are just not kept.
    """
    calls: list[ModelCall] = []
    token = _CALLS.set(calls)
    try:
        yield calls
    finally:
        _CALLS.reset(token)


def _record(provider: Provider, label: str | None, used_in: int, used_out: int) -> ModelCall:
    call = ModelCall(
        provider=provider.name,
        model=provider.model,
        label=label,
        input_tokens=used_in,
        output_tokens=used_out,
        cost_usd=cost_of(provider.name, provider.model, used_in, used_out),
    )
    if (calls := _CALLS.get()) is not None:
        calls.append(call)
    return call


def providers_from(settings: Settings) -> list[Provider]:
    """The providers named in order, keeping only those with what they need."""
    out: list[Provider] = []
    for raw in (settings.llm_providers or "").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name in _BASE_URLS:
            key = getattr(settings, f"{name}_api_key", "") or ""
            model = getattr(settings, f"{name}_model", "") or ""
            if key and model:
                out.append(Provider(name, _BASE_URLS[name], key, model))
        elif name == "ollama":
            if settings.ollama_base_url and settings.ollama_model:
                out.append(
                    Provider(
                        "ollama",
                        settings.ollama_base_url.rstrip("/"),
                        "ollama",
                        settings.ollama_model,
                    )
                )
        elif name == "anthropic":
            if settings.anthropic_api_key:
                out.append(
                    Provider(
                        "anthropic",
                        _ANTHROPIC_URL,
                        settings.anthropic_api_key,
                        settings.extract_model,
                    )
                )
        else:
            logger.warning("unknown model provider %r in LLM_PROVIDERS; skipped", name)
    return out


class TextModel:
    """Ask for a JSON object, from the first provider that will answer."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._providers = providers_from(settings)

    @property
    def configured(self) -> bool:
        return bool(self._providers)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self._providers]

    async def extract(
        self,
        *,
        instructions: str,
        text: str,
        shape: dict[str, Any],
        max_tokens: int = 4000,
        label: str | None = None,
    ) -> tuple[dict[str, Any], str] | None:
        """A JSON object in ``shape``, read from ``text``, and who answered.

        ``None`` when no provider is configured, every one refused, or nothing
        parseable came back. The caller treats all three the same way: as the
        deterministic reading standing alone.
        """
        system, body = self._prompt(instructions, text, shape)
        answer = await self.ask(system=system, user=body, max_tokens=max_tokens, label=label)
        return (answer.payload, answer.who) if answer else None

    @property
    def can_see(self) -> bool:
        """Whether a provider here can look at a page rather than read its
        text. Only Claude can; the free tiers are text only."""
        return any(p.name == "anthropic" for p in self._providers)

    async def see(
        self,
        *,
        instructions: str,
        file_name: str,
        media_type: str,
        data: bytes,
        shape: dict[str, Any],
        max_tokens: int = 4000,
        label: str | None = None,
    ) -> tuple[dict[str, Any], str] | None:
        """A JSON object in ``shape``, read off the file itself, and who answered.

        For what has no text to read — a scan, a photograph, a PDF printed from
        a web page with its table as a picture. Claude takes a PDF page by page
        and an image as it is, so the file goes as it came. ``None`` when Claude
        is not configured, refused, or answered nothing parseable, exactly as
        :meth:`extract`.
        """
        provider = next((p for p in self._providers if p.name == "anthropic"), None)
        if provider is None:
            return None
        system, body = self._prompt(instructions, "(the attached file)", shape)
        source = {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode(),
        }
        page = (
            {"type": "image", "source": source}
            if media_type.startswith("image/")
            else {"type": "document", "source": source, "title": file_name[:200]}
        )
        try:
            raw, used_in, used_out = await self._ask(
                provider, (system, [page, {"type": "text", "text": body}]), max_tokens
            )
        except ModelError as exc:
            logger.info("model %s declined to look at %s: %s", provider.name, file_name, exc)
            return None
        call = _record(provider, label, used_in, used_out)
        parsed = parse_json_object(raw)
        if parsed is None:
            logger.info("model %s answered nothing parseable for %s", provider.name, file_name)
            return None
        call.used = True
        return parsed, f"{provider.name}:{provider.model}"

    async def ask(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4000,
        claude_model: str | None = None,
        free_only: bool = False,
        check: Callable[[dict[str, Any]], bool] | None = None,
        label: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Answer | None:
        """One JSON object, from the providers in order.

        The free tiers come first in ``LLM_PROVIDERS``; Claude is asked only
        when every one before it refused — a rate limit, a spent daily
        allowance, an outage — or answered something unusable. ``check`` is
        the caller's test of an answer; one that fails it is treated as no
        answer, and the next provider is asked.

        ``claude_model`` overrides the model Claude is asked with, for a job
        that needs the main model rather than the extraction one.
        ``free_only`` leaves Claude out, for a caller with a Claude path of
        its own to fall back to. ``label`` says what is being read, on the
        record of the call kept by ``capture``.

        ``reasoning_effort`` ("low", "medium", "high") is passed to the
        gpt-oss models, which think before answering: low takes a short chat
        answer from about three seconds to one. Left out, each model does as
        it would — which is what reading a document wants.
        """
        for provider in self._providers:
            if provider.name == "anthropic":
                if free_only:
                    continue
                if claude_model:
                    provider = replace(provider, model=claude_model)
            try:
                raw, used_in, used_out = await self._ask(provider, (system, user), max_tokens, reasoning_effort)
            except ModelError as exc:
                logger.info("model %s declined: %s", provider.name, exc)
                continue
            call = _record(provider, label, used_in, used_out)
            parsed = parse_json_object(raw)
            if parsed is None:
                logger.info("model %s answered nothing parseable", provider.name)
                continue
            if check is not None and not check(parsed):
                logger.info("model %s answered outside the shape asked for", provider.name)
                continue
            call.used = True
            return Answer(parsed, f"{provider.name}:{provider.model}", used_in, used_out)
        return None

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        spoken: Spoken,
        order: str = "",
        max_tokens: int = 800,
        label: str | None = None,
        reasoning_effort: str | None = "low",
    ) -> AsyncIterator[str]:
        """A conversation's next reply, as plain text, a few words at a time.

        For a chat, where the first words matter more than the last: no JSON,
        and the words handed on as they come. ``messages`` are the turns so far
        ({"role": "user" | "assistant", "content"}), the last the person's.
        ``order`` names the providers to try ("cerebras,groq"), from those
        configured; blank keeps LLM_PROVIDERS' order. A provider that fails
        before its first word is passed over for the next; one that fails
        after it ends the reply where it stopped. ``spoken`` is filled in as
        it goes: who wrote it, the text, and how long the first word took.
        """
        providers = self._providers
        if order.strip():
            by_name = {p.name: p for p in self._providers}
            wanted = [n.strip().lower() for n in order.split(",") if n.strip()]
            providers = [by_name[n] for n in wanted if n in by_name]
        turns = _alternating(messages)
        started = time.monotonic()
        for provider in providers:
            used = [0, 0]
            try:
                pieces = self._chat_stream(
                    provider, system, turns, max_tokens, reasoning_effort, used
                )
                async for piece in pieces:
                    if spoken.first_ms is None:
                        spoken.first_ms = int((time.monotonic() - started) * 1000)
                        spoken.who = f"{provider.name}:{provider.model}"
                    spoken.text += piece
                    yield piece
            except ModelError as exc:
                if spoken.first_ms is None:
                    logger.info("model %s declined to chat: %s", provider.name, exc)
                    continue
                logger.warning("model %s stopped mid-reply: %s", provider.name, exc)
            if spoken.first_ms is None:
                # Answered nothing at all: as good as a refusal.
                logger.info("model %s said nothing", provider.name)
                continue
            _record(provider, label, used[0], used[1]).used = True
            break
        spoken.total_ms = int((time.monotonic() - started) * 1000)

    async def _chat_stream(
        self,
        provider: Provider,
        system: str,
        turns: list[dict[str, str]],
        max_tokens: int,
        reasoning_effort: str | None,
        used: list[int],
    ) -> AsyncIterator[str]:
        """One provider's streamed reply: its text pieces; tokens into ``used``."""
        timeout = httpx.Timeout(self._settings.llm_timeout_seconds, connect=10.0)
        if provider.name == "anthropic":
            url = provider.base_url
            headers = {
                "x-api-key": provider.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            if self._settings.anthropic_workspace_id:
                headers["anthropic-workspace-id"] = self._settings.anthropic_workspace_id
            body: dict[str, Any] = {
                "model": provider.model, "max_tokens": max_tokens, "system": system,
                "messages": turns, "stream": True,
            }
        else:
            url = f"{provider.base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {provider.api_key}",
                "content-type": "application/json",
                "HTTP-Referer": "https://hamdaz.com",
                "X-Title": "Hamdaz ERP",
            }
            body = {
                "model": provider.model,
                "messages": [{"role": "system", "content": system}, *turns],
                "temperature": 0.7,
                "max_tokens": max_tokens,
                "stream": True,
                **(
                    {"reasoning_effort": reasoning_effort}
                    if reasoning_effort and "gpt-oss" in provider.model
                    else {}
                ),
            }
        try:
            async with self._http.stream(
                "POST", url, headers=headers, json=body, timeout=timeout
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")[:160]
                    raise ModelError(f"{provider.name} answered {response.status_code}: {detail}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    piece = _stream_piece(provider.name, event, used)
                    if piece:
                        yield piece
        except httpx.HTTPError as exc:
            raise ModelError(f"could not reach {provider.name}: {exc}") from exc

    def _prompt(self, instructions: str, text: str, shape: dict[str, Any]) -> tuple[str, str]:
        system = (
            "You transcribe business documents into JSON for a procurement team. "
            "Copy values exactly as printed. Never calculate, never guess: use \"\" for "
            "text that is not on the document and 0 for an amount that is not. "
            "Reply with one JSON object and nothing else — no prose, no code fence."
        )
        body = (
            f"{instructions}\n\nUse exactly these keys:\n{json.dumps(shape, indent=1)}\n\n"
            f"DOCUMENT:\n{trim(text, self._settings.llm_max_input_chars)}"
        )
        return system, body

    async def _ask(
        self,
        provider: Provider,
        prompt: tuple[str, str | list[dict[str, Any]]],
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> tuple[str, int, int]:
        """The reply's text, and the tokens it took in and gave out. The body is
        a list of content blocks only for Claude, when a file goes with it."""
        system, body = prompt
        timeout = httpx.Timeout(self._settings.llm_timeout_seconds, connect=10.0)
        try:
            if provider.name == "anthropic":
                headers = {
                    "x-api-key": provider.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }
                # An identity-linked key names its workspace on every request.
                if self._settings.anthropic_workspace_id:
                    headers["anthropic-workspace-id"] = self._settings.anthropic_workspace_id
                response = await self._http.post(
                    provider.base_url,
                    headers=headers,
                    json={
                        "model": provider.model,
                        "max_tokens": max_tokens,
                        "system": system,
                        "messages": [{"role": "user", "content": body}],
                    },
                    timeout=timeout,
                )
            else:
                response = await self._http.post(
                    f"{provider.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {provider.api_key}",
                        "content-type": "application/json",
                        # OpenRouter asks for these two, and ignores them elsewhere.
                        "HTTP-Referer": "https://hamdaz.com",
                        "X-Title": "Hamdaz ERP",
                    },
                    json={
                        "model": provider.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": body},
                        ],
                        "temperature": 0,
                        "max_tokens": max_tokens,
                        "response_format": {"type": "json_object"},
                        # Only the gpt-oss models take it; others would refuse the field.
                        **(
                            {"reasoning_effort": reasoning_effort}
                            if reasoning_effort and "gpt-oss" in provider.model
                            else {}
                        ),
                    },
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise ModelError(f"could not reach {provider.name}: {exc}") from exc

        if response.status_code in _TRY_NEXT:
            raise ModelError(f"{provider.name} answered {response.status_code}")
        if response.status_code != 200:
            raise ModelError(
                f"{provider.name} refused ({response.status_code}): {response.text[:160]}"
            )
        payload = response.json()
        usage = payload.get("usage") or {}
        if provider.name == "anthropic":
            text = "".join(
                block.get("text", "")
                for block in payload.get("content", [])
                if block.get("type") == "text"
            )
            return (
                text,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
            )
        choices = payload.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}
        return (
            str(message.get("content") or ""),
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )


@dataclass(slots=True)
class Spoken:
    """What a streamed chat reply came to, filled in while it streams."""

    who: str = ""
    text: str = ""
    #: From the call to the first words, and to the last.
    first_ms: int | None = None
    total_ms: int = 0


def _alternating(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """The turns as every provider takes them: starting with the person, never
    two of one side in a row (Claude refuses either), none empty."""
    out: list[dict[str, str]] = []
    for m in messages:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = (m.get("content") or "").strip()
        if not content or (not out and role == "assistant"):
            continue
        if out and out[-1]["role"] == role:
            out[-1] = {"role": role, "content": f"{out[-1]['content']}\n\n{content}"}
        else:
            out.append({"role": role, "content": content})
    return out


def _stream_piece(provider: str, event: dict[str, Any], used: list[int]) -> str:
    """The text in one streamed event, counting the tokens it reports."""
    if provider == "anthropic":
        kind = event.get("type")
        if kind == "error":
            raise ModelError(f"anthropic stopped: {(event.get('error') or {}).get('message')}")
        if kind == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            used[0] = int(usage.get("input_tokens") or 0)
        elif kind == "message_delta":
            used[1] = int((event.get("usage") or {}).get("output_tokens") or 0)
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                return str(delta.get("text") or "")
        return ""
    if event.get("error"):
        raise ModelError(f"{provider} stopped: {event['error']}")
    # Groq reports usage under x_groq on its last event; others under usage.
    usage = event.get("usage") or (event.get("x_groq") or {}).get("usage")
    if usage:
        used[0] = int(usage.get("prompt_tokens") or 0)
        used[1] = int(usage.get("completion_tokens") or 0)
    choices = event.get("choices") or []
    delta = (choices[0].get("delta") if choices else None) or {}
    # Only the answer: a reasoning model's thinking comes as "reasoning".
    return str(delta.get("content") or "")


def trim(text: str, budget: int) -> str:
    """The document cut to ``budget`` characters: its head and its tail.

    The labelled fields are at the top and the totals at the bottom, so a long
    document keeps both ends and loses its middle, with a marker saying so.
    """
    if budget <= 0 or len(text) <= budget:
        return text
    head = int(budget * 0.7)
    tail = budget - head
    return text[:head] + "\n\n[… middle of the document left out …]\n\n" + text[-tail:]


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """The first JSON object in a model's reply, or ``None``."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
