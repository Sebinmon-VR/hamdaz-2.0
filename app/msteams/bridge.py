"""A Teams message to an AI employee, answered by the assistant as the person who sent it.

The message goes through **this app's own assistant routes**, in-process,
carrying a short session signed for the sender — the same way the assistant's
tools reach the modules. So everything the app enforces on a chat in the
browser holds here too: whether the person may use the assistant and this
employee, its rules and budget, the tools the person may use, and the
confirmation a change needs. Nothing about the sender is taken from Teams but
who they are (their Entra object id), and a sender with no account in this app
is told so and nothing else.

Three things differ from the browser:

* **Confirmations are words.** A person's yes or no to the next message stands
  for the buttons on screen; anything else is asked again.
* **Screen actions cannot run.** Opening a page or pressing a button needs the
  app in front of the person; the turn is told so and offers a link instead.
* **The answer arrives whole.** Teams shows it when it is ready.
"""

from __future__ import annotations

import html
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from app.auth.deps import SESSION_AUDIENCE
from app.core.config import Settings
from app.core.security import sign

logger = logging.getLogger("hamdaz.teams")

_TAG: Final = re.compile(r"<[^>]+>")
_AT: Final = re.compile(r"<at[^>]*>.*?</at>", re.I | re.S)
_YES: Final = frozenset(
    ["yes", "yeah", "yep", "ok", "okay", "sure", "confirm", "confirmed", "approve", "approved", "go", "proceed"]
)
_NO: Final = frozenset(["no", "nope", "cancel", "stop", "don't", "dont", "decline", "declined", "reject"])


def plain_text(body_html: str) -> str:
    """A Teams message body as the words a person typed, mentions removed."""
    text = _AT.sub(" ", body_html or "")
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = html.unescape(_TAG.sub("", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


def yes_or_no(text: str) -> bool | None:
    words = set(re.sub(r"[^a-z' ]", " ", text.lower()).split())
    yes, no = bool(words & _YES), bool(words & _NO)
    if yes == no:
        return None
    return yes


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _table(rows: list[str]) -> str:
    """A markdown table as the HTML table Teams draws."""
    head, body = _cells(rows[0]), [_cells(r) for r in rows[2:]]
    out = ["<table><thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for cells in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


_RULE: Final = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_BULLET: Final = "\u2022"


def to_html(markdown: str) -> str:
    """The assistant's answer as the small HTML a Teams message takes.

    Bold, code, links, bullets, headings, and tables — the assistant answers a
    list of tasks as a table, and Teams shows a markdown one as raw pipes.
    """
    lines = (markdown or "").strip().splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and _RULE.match(lines[i + 1]):
            rows = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        text = _inline(line)
        text = re.sub(r"^\s*[-*\u2022]\s+", _BULLET + " ", text)
        text = re.sub(r"^#{1,6}\s+(.*)$", r"<b>\1</b>", text)
        out.append(text)
        i += 1
    return "<br>".join(out) or "…"


@dataclass(slots=True)
class Turn:
    """What one assistant turn came to."""

    text: str = ""
    error: str | None = None
    #: The HTTP status of a refusal, when the route refused outright.
    status: int | None = None
    run_id: str | None = None
    confirm: list[dict[str, Any]] = field(default_factory=list)
    client: list[dict[str, Any]] = field(default_factory=list)


def parse_events(body: str) -> Turn:
    """The assistant's server-sent events, read whole."""
    turn = Turn()
    parts: list[str] = []
    for block in body.split("\n\n"):
        kind, data = "", ""
        for line in block.splitlines():
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if not kind or not data:
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if kind == "text":
            parts.append(str(payload.get("delta") or ""))
        elif kind == "error":
            turn.error = str(payload.get("message") or "Something went wrong.")
        elif kind == "confirm":
            turn.run_id = str(payload.get("run_id"))
            turn.confirm = list(payload.get("actions") or [])
        elif kind == "client_action":
            turn.run_id = str(payload.get("run_id"))
            turn.client = list(payload.get("actions") or [])
        elif kind == "run" and not turn.run_id:
            turn.run_id = str(payload.get("run_id"))
    turn.text = "".join(parts).strip()
    return turn


class AppCaller:
    """This app's own routes, in-process, as one person."""

    def __init__(self, app: Any, settings: Settings) -> None:
        self._app = app
        self._settings = settings

    def _client(self, user_id: uuid.UUID) -> httpx.AsyncClient:
        cookie = sign(
            {"sub": str(user_id)},
            secret=self._settings.session_secret,
            ttl_minutes=15,
            audience=SESSION_AUDIENCE,
        )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app, raise_app_exceptions=False),
            base_url="http://hamdaz.internal",
            cookies={self._settings.session_cookie_name: cookie},
            timeout=httpx.Timeout(300.0),
        )

    @staticmethod
    def _refusal(response: httpx.Response) -> str:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        return detail if isinstance(detail, str) else f"The assistant answered {response.status_code}."

    async def new_conversation(self, user_id: uuid.UUID, employee_id: uuid.UUID) -> uuid.UUID | str:
        """A new assistant conversation with the employee, or why not."""
        async with self._client(user_id) as client:
            response = await client.post(
                f"{self._settings.api_prefix}/assistant/conversations",
                json={"employee_id": str(employee_id), "title": "Teams chat"},
            )
        if response.status_code != 201:
            return self._refusal(response)
        return uuid.UUID(response.json()["id"])

    async def _stream(self, user_id: uuid.UUID, path: str, body: dict[str, Any]) -> Turn:
        async with self._client(user_id) as client:
            response = await client.post(f"{self._settings.api_prefix}{path}", json=body)
        if response.status_code != 200:
            return Turn(error=self._refusal(response), status=response.status_code)
        return parse_events(response.text)

    async def say(self, user_id: uuid.UUID, conversation_id: uuid.UUID, text: str) -> Turn:
        turn = await self._stream(
            user_id, f"/assistant/conversations/{conversation_id}/messages", {"text": text[:8000]}
        )
        return await self._settle_client(user_id, conversation_id, turn)

    async def decide(self, user_id: uuid.UUID, conversation_id: uuid.UUID, run_id: str, approved: bool) -> Turn:
        turn = await self._stream(
            user_id,
            f"/assistant/conversations/{conversation_id}/confirm",
            {"run_id": run_id, "approved": approved},
        )
        return await self._settle_client(user_id, conversation_id, turn)

    async def _settle_client(self, user_id: uuid.UUID, conversation_id: uuid.UUID, turn: Turn) -> Turn:
        """Screen actions cannot run in Teams: say so, and let the turn carry on."""
        text_so_far = turn.text
        for _ in range(3):
            if not turn.client or not turn.run_id:
                break
            link = self._settings.frontend_url.rstrip("/")
            results = [
                {
                    "call_id": str(action.get("call_id")),
                    "ok": False,
                    "output": (
                        "This person is talking to you in Microsoft Teams, not in the app, so "
                        "nothing on a screen can be opened or pressed. Answer in words, and give "
                        f"them a link into the app instead (it is at {link})."
                    ),
                }
                for action in turn.client
            ]
            turn = await self._stream(
                user_id,
                f"/assistant/conversations/{conversation_id}/client-result",
                {"run_id": turn.run_id, "results": results},
            )
            turn.text = " ".join(p for p in (text_so_far, turn.text) if p).strip()
            text_so_far = turn.text
        return turn


def reply_for(turn: Turn) -> str:
    """What the employee writes back, in markdown."""
    if turn.error:
        return f"Sorry — {turn.error}"
    text = turn.text or "Done."
    if turn.confirm:
        labels = "; ".join(str(a.get("label") or a.get("tool_key") or "this") for a in turn.confirm)
        text += (
            f"\n\nBefore I do this — **{labels}** — reply **yes** to go ahead or **no** to leave it."
        )
    return text
