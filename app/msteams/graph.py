"""Graph, as an AI employee's own Microsoft 365 account.

Microsoft lets an app send a Teams chat message only *as a signed-in user* —
there is no application permission for it outside migrations. So an AI
employee is a real account (luna@hamdaz.com), connected once by a super admin
who signs in as it; the app keeps that account's refresh token, encrypted, and
from then on reads its chats and writes in them as it.

Delegated scopes, asked for at connection: reading and writing the account's
chats and sending in them, its own profile, and ``offline_access`` for the
refresh token. Each must be added to
the app registration as a *delegated* permission and consented by an admin.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from typing import Any, Final
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings

logger = logging.getLogger("hamdaz.teams")

GRAPH: Final = "https://graph.microsoft.com/v1.0"
#: Only what Teams chat needs. Mail (Mail.ReadWrite, Mail.Send) is added when
#: an AI employee works by email; asking for it before then is a wider grant
#: for an admin to approve with nothing using it.
SCOPES: Final = (
    "offline_access",
    "User.Read",
    "Chat.ReadWrite",
    "ChatMessage.Send",
)
_REFRESH_BUFFER: Final = 120


class GraphAccountError(Exception):
    """The account could not be used. ``reconnect`` means a person must sign in again."""

    def __init__(self, message: str, *, reconnect: bool = False) -> None:
        super().__init__(message)
        self.reconnect = reconnect


def _fernet(settings: Settings) -> Fernet:
    key = hashlib.sha256(f"ai-employee-accounts:{settings.session_secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(settings: Settings, value: str) -> str:
    return _fernet(settings).encrypt(value.encode()).decode()


def decrypt(settings: Settings, value: str) -> str:
    try:
        return _fernet(settings).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise GraphAccountError("The stored sign-in cannot be read; connect the account again.", reconnect=True) from exc


class EmployeeGraph:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        #: Access tokens by employee id: (token, expires at, monotonic).
        self._tokens: dict[str, tuple[str, float]] = {}

    # ── connecting ─────────────────────────────────────────────────────

    def authorize_url(self, *, state: str, login_hint: str | None) -> str:
        params = {
            "client_id": self._settings.azure_client_id,
            "response_type": "code",
            "redirect_uri": self._settings.ai_connect_redirect_uri,
            "response_mode": "query",
            "scope": " ".join(SCOPES),
            "state": state,
            # Always ask which account: the super admin connecting it is signed
            # in as themselves, and their own account is the wrong answer.
            "prompt": "select_account",
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{self._settings.authority}/oauth2/v2.0/authorize?{urlencode(params)}"

    async def _token(self, data: dict[str, str]) -> dict[str, Any]:
        response = await self._http.post(
            f"{self._settings.authority}/oauth2/v2.0/token",
            data={
                "client_id": self._settings.azure_client_id,
                "client_secret": self._settings.azure_client_secret,
                "scope": " ".join(SCOPES),
                **data,
            },
        )
        payload = response.json() if response.content else {}
        if response.status_code != 200:
            code = payload.get("error", "")
            message = payload.get("error_description", "").split("\r\n")[0] or f"HTTP {response.status_code}"
            raise GraphAccountError(
                f"Microsoft refused the sign-in: {message}",
                reconnect=code in ("invalid_grant", "interaction_required", "consent_required"),
            )
        return payload

    async def redeem(self, code: str) -> dict[str, Any]:
        """The code from the redirect, for tokens."""
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.ai_connect_redirect_uri,
            }
        )

    async def access_token(self, employee_id: str, refresh_token: str) -> tuple[str, str | None]:
        """A current access token, and the new refresh token if Microsoft rotated it."""
        cached = self._tokens.get(employee_id)
        if cached and time.monotonic() < cached[1]:
            return cached[0], None
        payload = await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
        token = payload["access_token"]
        self._tokens[employee_id] = (
            token,
            time.monotonic() + int(payload.get("expires_in", 3600)) - _REFRESH_BUFFER,
        )
        return token, payload.get("refresh_token")

    def forget(self, employee_id: str) -> None:
        self._tokens.pop(employee_id, None)

    # ── calls ──────────────────────────────────────────────────────────

    async def _get(self, token: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = await self._http.get(
            f"{GRAPH}{path}", headers={"Authorization": f"Bearer {token}"}, params=params
        )
        if response.status_code == 401:
            raise GraphAccountError("Graph refused the account's token.", reconnect=True)
        if response.status_code != 200:
            raise GraphAccountError(f"Graph answered {response.status_code} for {path}: {response.text[:200]}")
        return response.json()

    async def me(self, token: str) -> dict[str, Any]:
        return await self._get(token, "/me", {"$select": "id,displayName,mail,userPrincipalName"})

    async def recent_chats(self, token: str) -> list[dict[str, Any]]:
        """The account's chats, most recently active first, with their last message."""
        payload = await self._get(
            token,
            "/me/chats",
            {
                "$expand": "lastMessagePreview",
                "$orderby": "lastMessagePreview/createdDateTime desc",
                "$top": "30",
            },
        )
        return payload.get("value", [])

    async def chat_exists(self, token: str, chat_id: str) -> bool:
        """Whether the account is in this chat. Reads its id and type, nothing said in it."""
        response = await self._http.get(
            f"{GRAPH}/me/chats/{chat_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"$select": "id,chatType"},
        )
        if response.status_code == 401:
            raise GraphAccountError("Graph refused the account's token.", reconnect=True)
        return response.status_code == 200

    async def messages(self, token: str, chat_id: str) -> list[dict[str, Any]]:
        """The latest messages in one chat, newest first."""
        payload = await self._get(
            token, f"/me/chats/{chat_id}/messages", {"$top": "20", "$orderby": "createdDateTime desc"}
        )
        return payload.get("value", [])

    async def send(self, token: str, chat_id: str, html: str) -> None:
        response = await self._http.post(
            f"{GRAPH}/chats/{chat_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"body": {"contentType": "html", "content": html}},
        )
        if response.status_code == 401:
            raise GraphAccountError("Graph refused the account's token.", reconnect=True)
        if response.status_code not in (200, 201):
            raise GraphAccountError(f"Teams refused the reply ({response.status_code}): {response.text[:200]}")
