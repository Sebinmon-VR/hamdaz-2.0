"""Getting a Zoho access token without minting more than one an hour.

Zoho allows a client only **10 active access tokens per refresh token** and
**10 token requests per 10 minutes**. Neither limit fails loudly: an eleventh
token silently invalidates the oldest, so a process that refreshes carelessly
logs out other processes and produces intermittent 401s that look like a
credentials problem and are not.

So a token is acquired through three layers, cheapest first:

1. **Process memory** — no I/O at all, which is where nearly every call lands.
2. **The ``zoho_token`` row**, taken under a Postgres advisory lock. A second
   instance blocks, then finds the token another instance just fetched and makes
   no network call.
3. **The network**, only when the stored token is genuinely near expiry.

The advisory lock rather than ``SELECT ... FOR UPDATE`` because there is nothing
to lock before the first row exists: two instances starting together would both
see "no row", both refresh, and burn two of the ten slots. An advisory lock is
held on a number, not a row, so it works on an empty table.

Steady state is one refresh per hour for the entire deployment, whatever the
instance count.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.zoho import ZohoToken

logger = logging.getLogger("hamdaz.zoho")

#: Treat a token as expired this long before Zoho does, so one cannot lapse
#: between the check and the call that uses it.
_SKEW_SECONDS: Final = 120

#: The advisory lock key. Arbitrary but fixed — any process using this number
#: for something else would deadlock against token refreshes.
_LOCK_KEY: Final = 0x7A6F686F  # "zoho"

#: Named in the error when the token endpoint rejects the client, because the
#: overwhelmingly likely cause is the wrong data centre rather than a bad secret.
_ACCOUNTS_HOSTS: Final = (
    "https://accounts.zoho.com",
    "https://accounts.zoho.eu",
    "https://accounts.zoho.in",
    "https://accounts.zoho.com.au",
    "https://accounts.zoho.jp",
    "https://accounts.zoho.ca",
    "https://accounts.zoho.sa",
    "https://accounts.zoho.com.cn",
)


class ZohoAuthError(Exception):
    """No access token could be obtained. Safe to show a user."""


@dataclass(frozen=True, slots=True)
class Token:
    value: str
    #: The API host for this data centre, as Zoho reported it.
    api_domain: str
    expires_at: datetime

    def fresh(self, *, now: datetime | None = None) -> bool:
        return self.expires_at > (now or datetime.now(UTC))


class TokenStore:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._http = http
        self._sessions = session_factory
        self._cached: Token | None = None
        # Guards the in-process case, so coroutines in one worker queue here
        # rather than on the database.
        self._lock = asyncio.Lock()

    async def get(self) -> Token:
        """A usable token, refreshing only if nobody else already has."""
        if not self._settings.zoho_configured:
            raise ZohoAuthError(
                "Zoho Books is not configured: set zoho_CLIENT_ID, zoho_CLIENT_SECRET, "
                "zoho_REFRESH_TOKEN and zoho_ORGANIZATION_ID."
            )

        if self._cached is not None and self._cached.fresh():
            return self._cached

        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if self._cached is not None and self._cached.fresh():
                return self._cached
            token = await self._shared()
            self._cached = token
            return token

    async def invalidate(self) -> None:
        """Forget the current token after Zoho rejected it.

        Clears the stored copy too — if Zoho says this token is dead, it is dead
        for every instance, and leaving it in the row means they each discover
        that separately.
        """
        self._cached = None
        async with self._sessions() as session, session.begin():
            row = await session.get(ZohoToken, 1)
            if row is not None:
                # Expire rather than delete: the timestamps on the row are the
                # record of how often this is happening.
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    # ── the shared layers ──────────────────────────────────────────────

    async def _shared(self) -> Token:
        """Layer 2 and 3: the row under a lock, then the network if need be."""
        async with self._sessions() as session, session.begin():
            # Held until this transaction commits. Anything else asking for a
            # token waits here rather than starting its own refresh.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            )

            row = await session.scalar(select(ZohoToken).where(ZohoToken.id == 1))
            if row is not None and row.expires_at > datetime.now(UTC):
                # Somebody else refreshed while we were queued. This is the
                # branch that makes running several instances cheap.
                return Token(row.access_token, row.api_domain, row.expires_at)

            fetched = await self._refresh()

            if row is None:
                session.add(
                    ZohoToken(
                        id=1,
                        access_token=fetched.value,
                        api_domain=fetched.api_domain,
                        expires_at=fetched.expires_at,
                    )
                )
            else:
                row.access_token = fetched.value
                row.api_domain = fetched.api_domain
                row.expires_at = fetched.expires_at

            return fetched

    async def _refresh(self) -> Token:
        """Layer 3. The only place that spends one of the ten token slots."""
        accounts = self._settings.zoho_accounts_url.rstrip("/")
        try:
            response = await self._http.post(
                f"{accounts}/oauth/v2/token",
                data={
                    "refresh_token": self._settings.zoho_refresh_token,
                    "client_id": self._settings.zoho_client_id,
                    "client_secret": self._settings.zoho_client_secret,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise ZohoAuthError(f"Could not reach {accounts}: {exc}") from exc

        if response.status_code != 200:
            raise ZohoAuthError(
                f"Zoho token endpoint returned {response.status_code}: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ZohoAuthError("Zoho token endpoint returned a non-JSON body") from exc

        # Zoho reports OAuth failures as HTTP 200 with an "error" key, so the
        # status code alone proves nothing.
        if error := payload.get("error"):
            raise ZohoAuthError(self._explain(str(error), accounts))

        access_token = payload.get("access_token")
        if not access_token:
            raise ZohoAuthError("Zoho returned no access_token")

        expires_in = int(payload.get("expires_in", 3600))
        expires_at = datetime.now(UTC) + timedelta(
            seconds=max(expires_in - _SKEW_SECONDS, 60)
        )

        api_domain = (payload.get("api_domain") or "").rstrip("/")
        if not api_domain:
            # Only seen when Zoho omits it. The accounts host is the best guess
            # available and at least keeps the data centre consistent.
            api_domain = accounts.replace("accounts.zoho", "www.zohoapis")

        logger.info("zoho token refreshed domain=%s expires_in=%ss", api_domain, expires_in)
        return Token(access_token, api_domain, expires_at)

    def _explain(self, error: str, accounts: str) -> str:
        if error == "invalid_client":
            others = ", ".join(h for h in _ACCOUNTS_HOSTS if h != accounts)
            return (
                f"Zoho rejected the client against {accounts} ({error}). This usually "
                f"means the organisation is in a different data centre — set "
                f"ZOHO_ACCOUNTS_URL to one of: {others}"
            )
        if error in ("invalid_code", "invalid_grant"):
            return (
                f"Zoho rejected the refresh token ({error}). It has been revoked, or "
                f"more than 20 refresh tokens were issued for this client and this one "
                f"was evicted. Generate a new one and set zoho_REFRESH_TOKEN."
            )
        return f"Zoho refused the token request: {error}"
