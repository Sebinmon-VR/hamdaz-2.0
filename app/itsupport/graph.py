"""The IT Support account's sign-in and its chat calls, through Graph.

**Delegated, as the account.** Graph does not let an application send a chat
message as a person, so the account signs in once with the backend's own app
registration, whose delegated chat permissions are already admin-consented:

* from the web, with **Connect** on /admin/it-support (``app.itsupport.router``):
  kept in the settings row (``itsupport_settings.sign_in``), so the server
  that runs the agent has it;
* or on a laptop, with ``python -m app.itsupport.connect``: kept in a local
  file (IT_SUPPORT_TOKEN_FILE).

Either way the refresh token is encrypted with a key from SESSION_SECRET. The
database's sign-in is used first; one this machine can't read (made by a
server with another SESSION_SECRET) falls back to the file. Access tokens are
made as needed; a new refresh token, when Microsoft rotates it, is written
back where it came from.
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


def _read_file(name: str) -> dict[str, Any] | None:
    path = Path(name)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def sign_in_record(settings: Settings, tokens: dict[str, Any], account: dict[str, Any],
                   ) -> dict[str, Any]:
    """The sign-in as kept: the refresh token encrypted, and whose it is."""
    return {
        "refresh_token": _fernet(settings).encrypt(tokens["refresh_token"].encode()).decode(),
        "account_id": account.get("id"),
        "account_email": (account.get("mail") or account.get("userPrincipalName") or "").lower(),
        "account_name": account.get("displayName"),
        "scope": tokens.get("scope", ""),
    }


class ITSupportGraph:
    """Graph as the IT Support account."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient,
                 factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._settings = settings
        self._http = http
        #: Where the database's sign-in is read from; None: the file only.
        self._factory = factory
        #: Where the sign-in in use came from: "db" or "file".
        self.source: str | None = None
        #: Access tokens by the scopes they were asked for, with their expiry.
        self._access: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        self.account_id: str | None = None

    def forget(self) -> None:
        """Drop the access tokens held, so the next call reads the sign-in
        again (after a Connect or Disconnect on the page)."""
        self._access.clear()

    async def _db_record(self) -> dict[str, Any] | None:
        if self._factory is None:
            return None
        from app.models.itsupport import ITSupportSettings  # no cycle at import

        try:
            async with self._factory() as session:
                row = await session.get(ITSupportSettings, 1)
                return dict(row.sign_in) if row is not None and row.sign_in else None
        except DBAPIError:
            return None  # the column not there yet: the file stands

    def _readable(self, record: dict[str, Any]) -> str | None:
        try:
            return _fernet(self._settings).decrypt(record["refresh_token"].encode()).decode()
        except (InvalidToken, KeyError):
            return None

    async def _record(self) -> tuple[dict[str, Any], str, str]:
        """The sign-in to use, where it came from, and its refresh token."""
        record = await self._db_record()
        if record is not None and (refresh := self._readable(record)) is not None:
            return record, "db", refresh
        kept = await asyncio.to_thread(_read_file, self._settings.it_support_token_file)
        if kept is not None:
            if (refresh := self._readable(kept)) is not None:
                return kept, "file", refresh
            raise NotConnectedError("the kept sign-in can't be read; connect again")
        if record is not None:
            raise NotConnectedError("the sign-in was made on a server with another "
                                    "SESSION_SECRET; connect again from this one")
        raise NotConnectedError("not connected: use Connect on the IT Support settings page")

    async def _keep(self, record: dict[str, Any], source: str) -> None:
        """A rotated refresh token, written back where it came from."""
        if source == "file":
            await asyncio.to_thread(
                Path(self._settings.it_support_token_file).write_text,
                json.dumps(record, indent=1), "utf-8",
            )
            return
        from app.models.itsupport import ITSupportSettings

        assert self._factory is not None
        async with self._factory() as session:
            row = await session.get(ITSupportSettings, 1)
            if row is not None and row.sign_in:
                row.sign_in = {**row.sign_in, "refresh_token": record["refresh_token"]}
                await session.commit()

    async def _token(self, scopes: str = SCOPES) -> str:
        async with self._lock:
            held = self._access.get(scopes)
            if held and time.monotonic() < held[1]:
                return held[0]
            record, source, refresh = await self._record()
            self.source = source
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
                await self._keep(record, source)
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

    async def one_to_one_chats(self) -> list[dict[str, Any]]:
        """The account's one-to-one chats with their members, for the
        settings page's chat viewer."""
        out: list[dict[str, Any]] = []
        payload = await self._call("GET", "/me/chats",
                                   params={"$expand": "members,lastMessagePreview",
                                           "$top": "50"})
        for _ in range(10):
            out.extend(payload.get("value") or [])
            more = payload.get("@odata.nextLink")
            if not more:
                break
            payload = await self._call("GET", more.removeprefix(GRAPH_BASE))
        return [c for c in out if c.get("chatType") == "oneOnOne"]

    async def message_page(self, chat_id: str, *, more: str | None = None,
                           top: int = 50) -> tuple[list[dict[str, Any]], str | None]:
        """A page of a chat's messages, newest first, and the link to the
        older ones (None at the start of the chat). ``more`` must be that
        link, for this chat."""
        if more:
            if not more.startswith(f"{GRAPH_BASE}/chats/{chat_id}/messages"):
                raise GraphError("that page link isn't this chat's")
            payload = await self._call("GET", more.removeprefix(GRAPH_BASE))
        else:
            payload = await self._call(
                "GET", f"/chats/{chat_id}/messages",
                params={"$top": str(top), "$orderby": "createdDateTime desc"})
        return list(payload.get("value") or []), payload.get("@odata.nextLink")

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

    async def send_files(self, chat_id: str, html_body: str,
                         cards: list[tuple[str, str, str]]) -> dict[str, Any]:
        """A message carrying several files already in SharePoint, each as
        its Teams file card: ``cards`` = (eTag GUID, the file's address, name)."""
        tags = "".join(f'<attachment id="{guid}"></attachment>' for guid, _, _ in cards)
        return await self._call(
            "POST", f"/chats/{chat_id}/messages",
            json={
                "body": {"contentType": "html", "content": f"{html_body}{tags}"},
                "attachments": [{"id": guid, "contentType": "reference", "contentUrl": url,
                                 "name": name} for guid, url, name in cards],
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
