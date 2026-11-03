"""Nova's line to Teams: the bot service's messages in, Nova's replies out.

**In.** Teams sends each message to ``POST /api/v1/nova/messages``, signed by
Microsoft's bot service. The token is checked properly before anything in the
message is believed — signature against the signer's published keys, issuer,
audience (our bot's id), expiry, the channel the key is endorsed for, and the
service URL it names. The address is public; an unchecked request would let
anyone speak as anyone.

**Out.** Replies and the typing dots go to the conversation's ``serviceUrl``,
with a token Entra gives for the bot's own id and secret.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.core.config import Settings

#: The bot service's own tokens.
BOT_SERVICE_ISSUER: Final = "https://api.botframework.com"
BOT_SERVICE_KEYS: Final = "https://login.botframework.com/v1/.well-known/keys"
#: What a token for talking to the bot service is asked for.
CONNECTOR_SCOPE: Final = "https://api.botframework.com/.default"

_KEYS_TTL_SECONDS: Final = 3600
_CLOCK_SKEW_SECONDS: Final = 300


class BotAuthError(Exception):
    """A message that can't be shown to come from Teams for our bot."""


class BotSendError(Exception):
    """A reply the bot service refused."""


@dataclass(frozen=True, slots=True)
class Address:
    """Where a reply goes, and who it is from and to: the conversation a
    message came in on, its two ends swapped. The bot service refuses an
    activity that does not say who it is from."""

    service_url: str
    conversation_id: str
    #: The bot's account in this conversation, and the person's: {id, name}.
    bot: dict[str, str]
    person: dict[str, str]

    @classmethod
    def of(cls, activity: dict[str, Any]) -> Address | None:
        service_url = activity.get("serviceUrl")
        conversation_id = (activity.get("conversation") or {}).get("id")
        if not service_url or not conversation_id:
            return None

        def account(value: Any) -> dict[str, str]:
            value = value or {}
            return {k: str(value[k]) for k in ("id", "name") if value.get(k)}

        return cls(service_url, conversation_id,
                   account(activity.get("recipient")), account(activity.get("from")))


class TeamsBot:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._keys: dict[str, tuple[float, dict[str, Any]]] = {}
        self._token: tuple[str, float] | None = None

    @property
    def configured(self) -> bool:
        s = self._settings
        return bool(s.nova_app_id and s.nova_app_secret and s.azure_tenant_id)

    def _signers(self) -> dict[str, str]:
        """Who may sign a message to us, and where their keys are: the bot
        service, and — for a single-tenant bot — our own tenant's Entra."""
        tenant = self._settings.azure_tenant_id
        entra_keys = f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
        return {
            BOT_SERVICE_ISSUER: BOT_SERVICE_KEYS,
            f"https://sts.windows.net/{tenant}/": entra_keys,
            f"https://login.microsoftonline.com/{tenant}/v2.0": entra_keys,
        }

    # ── in ──────────────────────────────────────────────────────────────

    async def verify(
        self, authorization: str | None, *, service_url: str | None, channel_id: str | None
    ) -> dict[str, Any]:
        """The token's claims, once it is shown to be the bot service (or our
        tenant) writing to our bot about this conversation."""
        if not authorization or not authorization.startswith("Bearer "):
            raise BotAuthError("no bearer token")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            issuer = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError as exc:
            raise BotAuthError(f"malformed token: {exc}") from exc
        keys_url = self._signers().get(issuer or "")
        if keys_url is None:
            raise BotAuthError(f"issuer {issuer!r} is not trusted")
        if not kid:
            raise BotAuthError("token header has no kid")
        jwk = await self._key(keys_url, kid)
        endorsed = jwk.get("endorsements")
        if endorsed is not None and channel_id and channel_id not in endorsed:
            raise BotAuthError(f"key not endorsed for channel {channel_id!r}")
        try:
            claims = jwt.decode(
                token,
                RSAAlgorithm.from_jwk(jwk),
                algorithms=["RS256"],
                audience=self._settings.nova_app_id,
                issuer=issuer,
                leeway=_CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise BotAuthError(f"token refused: {exc}") from exc
        named = claims.get("serviceurl") or claims.get("serviceUrl")
        if named and service_url and named.rstrip("/") != service_url.rstrip("/"):
            raise BotAuthError("token names another service URL")
        return claims

    async def _key(self, url: str, kid: str) -> dict[str, Any]:
        for force in (False, True):
            keys = await self._jwks(url, force=force)
            found = next((k for k in keys.get("keys", []) if k.get("kid") == kid), None)
            if found is not None:
                return found
            # Keys rotate; a miss usually means the cached set is stale.
        raise BotAuthError(f"no signing key matches kid {kid!r}")

    async def _jwks(self, url: str, *, force: bool = False) -> dict[str, Any]:
        cached = self._keys.get(url)
        if cached and not force and time.monotonic() - cached[0] < _KEYS_TTL_SECONDS:
            return cached[1]
        response = await self._http.get(url)
        if response.status_code != 200:
            raise BotAuthError(f"could not fetch signing keys ({response.status_code})")
        keys = response.json()
        self._keys[url] = (time.monotonic(), keys)
        return keys

    # ── out ─────────────────────────────────────────────────────────────

    async def _connector_token(self) -> str:
        if self._token and time.monotonic() < self._token[1]:
            return self._token[0]
        s = self._settings
        response = await self._http.post(
            f"https://login.microsoftonline.com/{s.azure_tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": s.nova_app_id,
                "client_secret": s.nova_app_secret,
                "scope": CONNECTOR_SCOPE,
            },
        )
        if response.status_code != 200:
            raise BotSendError(f"Entra refused the bot a token ({response.status_code})")
        payload = response.json()
        self._token = (
            payload["access_token"],
            time.monotonic() + int(payload.get("expires_in", 3600)) - 300,
        )
        return self._token[0]

    async def send(self, to: Address, activity: dict[str, Any]) -> str:
        """Post one activity — a message, or the typing dots — to a
        conversation, from the bot to the person; its id."""
        url = (
            f"{to.service_url.rstrip('/')}/v3/conversations/"
            f"{quote(to.conversation_id, safe='')}/activities"
        )
        body = {
            **activity,
            "from": to.bot,
            "recipient": to.person,
            "conversation": {"id": to.conversation_id},
        }
        response = await self._http.post(
            url, json=body,
            headers={"Authorization": f"Bearer {await self._connector_token()}"},
        )
        if response.status_code == 401:
            self._token = None
        if response.status_code >= 400:
            raise BotSendError(
                f"the bot service refused ({response.status_code}): {response.text[:200]}"
            )
        try:
            return str(response.json().get("id") or "")
        except ValueError:
            return ""

    async def say(self, to: Address, text: str) -> str:
        return await self.send(to, {"type": "message", "text": text, "textFormat": "markdown"})

    async def typing(self, to: Address) -> None:
        await self.send(to, {"type": "typing"})
