"""The one place the assistant talks to OpenAI.

A thin wrapper so the agent loop depends on an interface — ``stream(...)``
yielding Responses API events — that a test can stand in for without a key or
a network. The client is created lazily: an unset key fails at the first turn
with a message a super admin can act on, rather than stopping the app booting
for everyone who never opens the assistant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI

from app.assistant.catalogue import MODELS_BY_KEY
from app.core.config import Settings


class LLMError(Exception):
    """OpenAI could not be used. The message is safe to show a person."""


class OpenAIChat:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncOpenAI | None = None

    @property
    def configured(self) -> bool:
        return self._settings.openai_configured

    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self._settings.openai_api_key:
                raise LLMError(
                    "The assistant needs an OpenAI API key. Create one at "
                    "platform.openai.com and set OPENAI_API_KEY."
                )
            self._client = AsyncOpenAI(
                api_key=self._settings.openai_api_key,
                base_url=self._settings.openai_base_url or None,
                timeout=self._settings.openai_timeout_seconds,
                max_retries=2,
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
        """Start one model call and yield its streaming events.

        ``store=False``: the transcript is ours, held in Postgres for audit, so
        nothing needs to persist on OpenAI's side. Reasoning is carried between
        tool rounds by echoing the encrypted reasoning items back, which is why
        they are asked for here.
        """
        params: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": max_output_tokens,
            "truncation": "auto",
            "prompt_cache_key": user_key,
            "safety_identifier": user_key,
            "stream": True,
        }
        # Not every model thinks before answering. The GPT-4 family rejects
        # this parameter rather than ignoring it, which would fail the whole
        # turn, so it is only sent where it means something.
        spec = MODELS_BY_KEY.get(model)
        if spec is None or spec.supports_reasoning:
            params["reasoning"] = {"effort": reasoning_effort}
        try:
            stream = await self.client().responses.create(**params)
        except openai.OpenAIError as exc:
            raise LLMError(explain(exc)) from exc
        return stream


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
        """One call, one answer, no tools and no stream.

        The turn loop above exists because the assistant does not know what it
        will need — it asks for tools, reads results and goes round again.
        Summarising something the caller is already holding is the opposite
        problem: the whole input is known, one call is enough, and streaming a
        result nobody watches being written only adds ways to fail halfway.

        Returns the text and the token counts, because a feature that spends
        money per use should be able to say how much.
        """
        params: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": prompt,
            "store": False,
            "max_output_tokens": max_output_tokens,
            "truncation": "auto",
            "safety_identifier": user_key,
        }
        if schema is not None:
            params["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "answer",
                    "strict": True,
                    "schema": schema,
                }
            }
        spec = MODELS_BY_KEY.get(model)
        if spec is None or spec.supports_reasoning:
            params["reasoning"] = {"effort": reasoning_effort}
        try:
            response = await self.client().responses.create(**params)
        except openai.OpenAIError as exc:
            raise LLMError(explain(exc)) from exc

        usage = getattr(response, "usage", None)
        return (
            (getattr(response, "output_text", "") or "").strip(),
            int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0),
        )

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
        """One answer that may look things up on the web first.

        ``answer`` above is for summarising what the caller already holds. This
        is for the question whose answer is out there — which distributor in
        the UAE stocks a given valve — and the model is given the web search
        tool and left to use it. Still one call from our side: the searching
        happens inside the response, and what comes back is the text (or the
        JSON the schema asks for) and the token counts.
        """
        params: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": prompt,
            "store": False,
            "max_output_tokens": max_output_tokens,
            "truncation": "auto",
            "safety_identifier": user_key,
        }
        if web_search:
            params["tools"] = [{"type": "web_search"}]
        if schema is not None:
            params["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "answer",
                    "strict": True,
                    "schema": schema,
                }
            }
        spec = MODELS_BY_KEY.get(model)
        if spec is None or spec.supports_reasoning:
            params["reasoning"] = {"effort": reasoning_effort}
        try:
            response = await self.client().responses.create(**params)
        except openai.OpenAIError as exc:
            raise LLMError(explain(exc)) from exc
        usage = getattr(response, "usage", None)
        return (
            (getattr(response, "output_text", "") or "").strip(),
            int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0),
        )

    async def speak(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        instructions: str | None,
        response_format: str = "mp3",
    ) -> AsyncIterator[bytes]:
        """Read ``text`` aloud, yielding audio as it is generated.

        Streamed rather than returned whole so playback can start on the first
        chunk. A spoken answer generated in full before anything is heard feels
        broken even when it is fast, and the wait grows with the sentence.

        ``instructions`` steers tone and pace and is what separates this from a
        flat reading. Only ``gpt-4o-mini-tts`` acts on it; the older ``tts-1``
        engines ignore it rather than failing, so it is always sent.
        """
        try:
            async with self.client().audio.speech.with_streaming_response.create(
                model=model,
                voice=voice,
                input=text,
                instructions=instructions or openai.omit,
                response_format=response_format,
            ) as response:
                async for chunk in response.iter_bytes():
                    yield chunk
        except openai.OpenAIError as exc:
            raise LLMError(explain(exc)) from exc


    async def realtime_secret(
        self,
        *,
        model: str,
        instructions: str,
        tools: list[dict[str, Any]],
        voice: str,
        seconds: int,
    ) -> tuple[str, int]:
        """Mint a short-lived token the browser can open a session with.

        The session is *defined here* — model, instructions, tool list and voice
        are all fixed at mint time. The browser receives a key to a room it did
        not furnish, which is what stops a tampered client from giving itself a
        tool the person may not use.

        Returns the secret and the epoch second it stops working.
        """
        try:
            secret = await self.client().realtime.client_secrets.create(
                expires_after={"anchor": "created_at", "seconds": seconds},
                session={
                    "type": "realtime",
                    "model": model,
                    "instructions": instructions,
                    "tools": tools,
                    "tool_choice": "auto",
                    "audio": {
                        "output": {"voice": voice},
                        # Transcribing the microphone as well is what lets the
                        # screen show what a person said. Without it a spoken
                        # conversation leaves no readable trace of their half.
                        "input": {"transcription": {"model": "gpt-4o-mini-transcribe","language": "en"}},
                    },
                },
            )
        except openai.OpenAIError as exc:
            raise LLMError(explain(exc)) from exc
        return secret.value, int(secret.expires_at)


def explain(exc: Exception) -> str:
    """Turn an OpenAI error into something a person can act on."""
    if isinstance(exc, openai.AuthenticationError):
        return "OpenAI rejected the API key. Ask a super admin to check OPENAI_API_KEY."
    if isinstance(exc, openai.PermissionDeniedError):
        return "The OpenAI key is not allowed to use this model. Ask a super admin."
    if isinstance(exc, openai.RateLimitError):
        return "OpenAI is rate limiting this key. Try again shortly."
    if isinstance(exc, openai.NotFoundError):
        return "OpenAI does not know the configured model. Ask a super admin to pick another."
    if isinstance(exc, openai.BadRequestError):
        return f"OpenAI rejected the request: {exc.message}"
    if isinstance(exc, openai.APITimeoutError):
        return "OpenAI took too long to answer. Try again."
    if isinstance(exc, openai.APIConnectionError):
        return "Could not reach OpenAI. Try again shortly."
    if isinstance(exc, openai.APIStatusError):
        return f"OpenAI returned an error ({exc.status_code}). Try again shortly."
    return f"The assistant hit an unexpected error: {exc}"
