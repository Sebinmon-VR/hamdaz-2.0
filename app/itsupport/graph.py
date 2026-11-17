"""The IT Support account's sign-in and its chat calls, through Graph.

**Delegated, as the account.** Graph does not let an application send a chat
message as a person, so the account signs in once (``app.itsupport.connect``)
with the backend's own app registration, whose delegated chat permissions are
already admin-consented. The refresh token is kept in a local file, encrypted
with a key from SESSION_SECRET, and turned into access tokens as needed; a
new refresh token, when Microsoft rotates it, is written back.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Final

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.core.mail import GRAPH_BASE

logger = logging.getLogger("hamdaz.itsupport")

#: What the account is asked for; all admin-consented for the backend's app.
#: Chat.Create finds a one-to-one chat (it returns the existing one).
SCOPES: Final = (
    "openid profile offline_access User.Read User.Read.All Chat.ReadWrite Chat.Create "
    "ChatMessage.Send"
)
#: Reading a file someone sends in the chat. A Teams chat file lives in the
#: sender's OneDrive, shared with the chat's members, so the account opens it
#: with its own read-only file permission: only what was shared with it, the
#: way a person in the chat opens it. Asked for its own token, so a sign-in
#: made before this existed keeps chatting and only files are refused.
FILE_SCOPES: Final = "offline_access Files.Read.All"
#: What ``connect`` asks consent for: both, in one sign-in.
CONNECT_SCOPES: Final = f"{SCOPES} Files.Read.All"
#: A file larger than this is not downloaded.
MAX_FILE_BYTES: Final = 25 * 1024 * 1024


class GraphError(Exception):
    """A Graph call that failed; the worker logs it and tries again later."""


class NotConnectedError(GraphError):
    """No sign-in kept for the account on this machine."""


class FilesNotAllowedError(GraphError):
    """The kept sign-in can't open chat files yet: Files.Read.All was not
    consented. ``python -m app.itsupport.connect`` again asks for it."""


class FileTooBigError(GraphError):
    pass


def _fernet(settings: Settings) -> Fernet:
    digest = hashlib.sha256(b"itsupport-teams:" + settings.session_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def save_sign_in(settings: Settings, tokens: dict[str, Any], account: dict[str, Any]) -> Path:
    """Keep the account's refresh token, encrypted, in the token file."""
    path = Path(settings.it_support_token_file)
    record = {
        "refresh_token": _fernet(settings).encrypt(tokens["refresh_token"].encode()).decode(),
        "account_id": account.get("id"),
        "account_email": (account.get("mail") or account.get("userPrincipalName") or "").lower(),
        "scope": tokens.get("scope", ""),
    }
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return path


class ITSupportGraph:
    """Graph as the IT Support account."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        #: Access tokens by the scopes they were asked for, with their expiry.
        self._access: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        self.account_id: str | None = None

    def _record(self) -> dict[str, Any]:
        path = Path(self._settings.it_support_token_file)
        if not path.exists():
            raise NotConnectedError(f"no sign-in at {path}; run: python -m app.itsupport.connect")
        return json.loads(path.read_text(encoding="utf-8"))

    async def _token(self, scopes: str = SCOPES) -> str:
        async with self._lock:
            held = self._access.get(scopes)
            if held and time.monotonic() < held[1]:
                return held[0]
            record = self._record()
            try:
                refresh = _fernet(self._settings).decrypt(record["refresh_token"].encode()).decode()
            except (InvalidToken, KeyError) as exc:
                raise NotConnectedError("the kept sign-in can't be read; connect again") from exc
            s = self._settings
            response = await self._http.post(
                f"{s.authority}/oauth2/v2.0/token",
                data={
                    "client_id": s.azure_client_id,
                    "client_secret": s.azure_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "scope": scopes,
                },
            )
            if response.status_code != 200 and scopes != SCOPES:
                raise FilesNotAllowedError(
                    f"Microsoft refused file access ({response.status_code}): "
                    f"{response.text[:200]}"
                )
            if response.status_code != 200:
                raise NotConnectedError(
                    f"Microsoft refused the kept sign-in ({response.status_code}): "
                    f"{response.text[:200]}"
                )
            payload = response.json()
            if payload.get("refresh_token") and payload["refresh_token"] != refresh:
                record["refresh_token"] = (
                    _fernet(s).encrypt(payload["refresh_token"].encode()).decode()
                )
                await asyncio.to_thread(
                    Path(s.it_support_token_file).write_text,
                    json.dumps(record, indent=1), "utf-8",
                )
            self.account_id = record.get("account_id")
            self._access[scopes] = (
                payload["access_token"],
                time.monotonic() + int(payload.get("expires_in", 3600)) - 300,
            )
            return payload["access_token"]

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._http.request(
            method, f"{GRAPH_BASE}{path}",
            headers={"Authorization": f"Bearer {await self._token()}"}, **kwargs,
        )
        if response.status_code == 401:
            self._access.pop(SCOPES, None)
        if response.status_code == 429:
            wait = float(response.headers.get("Retry-After") or 5)
            raise GraphError(f"throttled, retry after {wait}s")
        if response.status_code >= 400:
            raise GraphError(f"{method} {path} -> {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

    async def me(self) -> dict[str, Any]:
        return await self._call(
            "GET", "/me", params={"$select": "id,displayName,mail,userPrincipalName"}
        )

    async def user(self, email: str) -> dict[str, Any]:
        return await self._call(
            "GET", f"/users/{email}", params={"$select": "id,displayName,mail"}
        )

    async def chat_with(self, me_id: str, person_id: str) -> str:
        """The one-to-one chat between the account and a person: Graph gives
        back the existing one rather than making another."""
        def member(user_id: str) -> dict[str, Any]:
            return {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"{GRAPH_BASE}/users('{user_id}')",
            }

        chat = await self._call(
            "POST", "/chats",
            json={"chatType": "oneOnOne", "members": [member(me_id), member(person_id)]},
        )
        return str(chat["id"])

    async def messages(self, chat_id: str, top: int) -> list[dict[str, Any]]:
        """The chat's newest messages, newest first."""
        payload = await self._call(
            "GET", f"/chats/{chat_id}/messages",
            params={"$top": str(top), "$orderby": "createdDateTime desc"},
        )
        return list(payload.get("value") or [])

    async def every_message(self, chat_id: str) -> list[dict[str, Any]]:
        """The whole chat, page by page, newest first."""
        out: list[dict[str, Any]] = []
        payload = await self._call("GET", f"/chats/{chat_id}/messages", params={"$top": "50"})
        while True:
            out.extend(payload.get("value") or [])
            more = payload.get("@odata.nextLink")
            if not more:
                return out
            payload = await self._call("GET", more.removeprefix(GRAPH_BASE))

    async def delete_own(self, chat_id: str, message_id: str) -> None:
        """Delete a message the account sent (soft delete: Teams shows "This
        message has been deleted"). Only the account's own messages."""
        if self.account_id is None:
            await self._token()
        await self._call(
            "POST", f"/users/{self.account_id}/chats/{chat_id}/messages/{message_id}/softDelete"
        )

    async def edit(self, chat_id: str, message_id: str, html_body: str) -> None:
        """Change a message the account sent (Chat.ReadWrite, delegated)."""
        await self._call(
            "PATCH", f"/chats/{chat_id}/messages/{message_id}",
            json={"body": {"contentType": "html", "content": html_body}},
        )

    async def send(self, chat_id: str, html_body: str) -> dict[str, Any]:
        return await self._call(
            "POST", f"/chats/{chat_id}/messages",
            json={"body": {"contentType": "html", "content": html_body}},
        )

    async def send_file(self, chat_id: str, html_body: str, *, name: str, url: str,
                        attachment_id: str) -> dict[str, Any]:
        """A message carrying a file that is already in SharePoint or OneDrive
        (a ``reference`` attachment: Teams shows its file card). Nothing is
        uploaded here; ``attachment_id`` is the GUID in the file's eTag."""
        return await self._call(
            "POST", f"/chats/{chat_id}/messages",
            json={
                "body": {"contentType": "html",
                         "content": f'{html_body}<attachment id="{attachment_id}"></attachment>'},
                "attachments": [{"id": attachment_id, "contentType": "reference",
                                 "contentUrl": url, "name": name}],
            },
        )

    # ── files sent in the chat ─────────────────────────────────────────

    async def _bytes(self, url: str, token: str) -> bytes:
        response = await self._http.get(
            url, headers={"Authorization": f"Bearer {token}"}, follow_redirects=True,
            timeout=120,
        )
        if response.status_code in (401, 403):
            raise FilesNotAllowedError(f"GET {url[:80]} -> {response.status_code}")
        if response.status_code >= 400:
            raise GraphError(f"GET {url[:80]} -> {response.status_code}: {response.text[:200]}")
        if len(response.content) > MAX_FILE_BYTES:
            raise FileTooBigError(f"{len(response.content) // 1_048_576} MB")
        return response.content

    async def shared_file(self, content_url: str) -> bytes:
        """A file attached to a chat message (its ``contentUrl``, in the
        sender's OneDrive), opened as shared with the account."""
        token = await self._token(FILE_SCOPES)
        share = "u!" + base64.urlsafe_b64encode(content_url.encode()).decode().rstrip("=")
        meta = await self._http.get(
            f"{GRAPH_BASE}/shares/{share}/driveItem", params={"$select": "name,size"},
            headers={"Authorization": f"Bearer {token}"},
        )
        if meta.status_code in (401, 403):
            raise FilesNotAllowedError(f"driveItem -> {meta.status_code}: {meta.text[:200]}")
        if meta.status_code >= 400:
            raise GraphError(f"driveItem -> {meta.status_code}: {meta.text[:200]}")
        if int(meta.json().get("size") or 0) > MAX_FILE_BYTES:
            raise FileTooBigError(f"{int(meta.json()['size']) // 1_048_576} MB")
        return await self._bytes(f"{GRAPH_BASE}/shares/{share}/driveItem/content", token)

    async def hosted_content(self, url: str) -> bytes:
        """A picture pasted into a message (``.../hostedContents/{id}/$value``),
        read with the chat permission."""
        if not url.startswith(GRAPH_BASE):
            raise GraphError("not a Graph hosted-content address")
        return await self._bytes(url, await self._token())
