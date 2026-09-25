"""A text model, when one is wanted, behind whichever free endpoint is up.

Deterministic readers do the work in this application. This is the second
pass they hand a document to when the words and tables did not settle it: a
supplier quotation laid out in a way the table reader cannot follow, a tender
whose requirements are paragraphs rather than rows. It is optional, it is
text only, and everything it answers goes through the same validation the
parsers' answers do.

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

import json
import logging
import re
from dataclasses import dataclass
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
    ) -> tuple[dict[str, Any], str] | None:
        """A JSON object in ``shape``, read from ``text``, and who answered.

        ``None`` when no provider is configured, every one refused, or nothing
        parseable came back. The caller treats all three the same way: as the
        deterministic reading standing alone.
        """
        if not self._providers:
            return None
        prompt = self._prompt(instructions, text, shape)
        for provider in self._providers:
            try:
                raw = await self._ask(provider, prompt, max_tokens)
            except ModelError as exc:
                logger.info("model %s declined: %s", provider.name, exc)
                continue
            parsed = parse_json_object(raw)
            if parsed is None:
                logger.info("model %s answered nothing parseable", provider.name)
                continue
            return parsed, f"{provider.name}:{provider.model}"
        return None

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

    async def _ask(self, provider: Provider, prompt: tuple[str, str], max_tokens: int) -> str:
        system, body = prompt
        timeout = httpx.Timeout(self._settings.llm_timeout_seconds, connect=10.0)
        try:
            if provider.name == "anthropic":
                response = await self._http.post(
                    provider.base_url,
                    headers={
                        "x-api-key": provider.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
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
        if provider.name == "anthropic":
            return "".join(
                block.get("text", "")
                for block in payload.get("content", [])
                if block.get("type") == "text"
            )
        choices = payload.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}
        return str(message.get("content") or "")


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
