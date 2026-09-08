"""Turning a tool call into a request to the app's own route.

The request is made in-process — the ASGI app is called directly, no socket —
and carries the caller's session cookie. Everything the route would check for a
browser it checks here: the session, the global roles, team membership, the HR
team, the finance role set. A 403 comes back as a 403, and the model is told.

This is deliberately the *only* way the assistant reaches a module. There is no
shortcut into a service function, because a shortcut would have to reimplement
the route's guard, and two copies of a permission check are how one of them
goes stale.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI

from app.assistant.catalogue import ToolSpec

logger = logging.getLogger("hamdaz.assistant")


@dataclass(slots=True)
class ToolOutcome:
    status: int
    ok: bool
    #: What the model is given: the JSON body, or an error the model can relay.
    text: str
    truncated: bool
    ms: int
    #: Parsed body when it was JSON, for the event log and the UI.
    body: Any = None


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class ToolExecutor:
    def __init__(
        self,
        app: FastAPI,
        *,
        api_prefix: str,
        cookie_name: str,
        max_chars: int = 12_000,
        timeout: float = 60.0,
    ) -> None:
        self._app = app
        self._prefix = api_prefix.rstrip("/")
        self._cookie_name = cookie_name
        self._max_chars = max_chars
        self._timeout = timeout

    def build(self, spec: ToolSpec, arguments: dict[str, Any]) -> tuple[str, dict, Any]:
        """(url, query, json body) for a call. Raises ValueError on a bad argument."""
        path_values, query, body = spec.split(arguments)
        path = spec.path
        for name, value in path_values.items():
            path = path.replace("{" + name + "}", quote(value, safe=""))
        if "{" in path:
            raise ValueError(f"{spec.key}: path {spec.path!r} still has unfilled segments")
        return (
            self._prefix + path,
            {k: _query_value(v) for k, v in query.items()},
            body if spec.method != "GET" and body else None,
        )

    async def call(
        self, spec: ToolSpec, arguments: dict[str, Any], *, session_cookie: str
    ) -> ToolOutcome:
        try:
            url, query, body = self.build(spec, arguments)
        except ValueError as exc:
            return ToolOutcome(0, False, f"Bad arguments: {exc}", False, 0)

        started = time.perf_counter()
        transport = httpx.ASGITransport(app=self._app, raise_app_exceptions=False)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://assistant.internal",
                cookies={self._cookie_name: session_cookie},
                timeout=httpx.Timeout(self._timeout),
            ) as client:
                response = await client.request(
                    spec.method, url, params=query or None, json=body
                )
        except httpx.HTTPError as exc:
            ms = int((time.perf_counter() - started) * 1000)
            logger.warning("tool %s failed: %s", spec.key, exc)
            return ToolOutcome(0, False, f"The request failed: {exc}", False, ms)
        ms = int((time.perf_counter() - started) * 1000)

        parsed: Any = None
        content_type = response.headers.get("content-type", "")
        raw = response.text
        if "json" in content_type:
            try:
                parsed = response.json()
            except ValueError:
                parsed = None

        if response.is_success:
            text = json.dumps(parsed, default=str) if parsed is not None else (raw or "(empty)")
        else:
            detail = parsed.get("detail") if isinstance(parsed, dict) else None
            text = json.dumps(
                {"error": detail or raw or response.reason_phrase, "status": response.status_code}
            )

        truncated = len(text) > self._max_chars
        if truncated:
            omitted = len(text) - self._max_chars
            text = text[: self._max_chars] + f"\n[truncated: {omitted} more characters]"
        return ToolOutcome(response.status_code, response.is_success, text, truncated, ms, parsed)
