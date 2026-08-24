"""Azure AD (Entra ID) OpenID Connect.

Microsoft stays the identity provider — the same SSO everyone already uses. What changes is
that it now only answers *who are you*. Authorization comes from Postgres (§4), not from a
SharePoint list read at boot.

The ID token is verified properly: signature against the tenant's published JWKS, plus
issuer, audience and expiry. The legacy app decoded tokens without verification in places.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt

from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger

logger = get_logger(__name__)

GRAPH_ME: Final = "https://graph.microsoft.com/v1.0/me"
#: openid/profile/email identify the user; User.Read lets us fetch the display name.
DEFAULT_SCOPES: Final = ("openid", "profile", "email", "User.Read")
_JWKS_TTL_SECONDS: Final = 3600


@dataclass(frozen=True, slots=True)
class OIDCUser:
    """The identity claims we trust after verification."""

    object_id: str
    email: str
    display_name: str


class AzureOIDC:
    """Authorization-code flow with PKCE."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        self._owns_http = http is None
        self._jwks: dict[str, Any] | None = None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @property
    def _authority(self) -> str:
        return f"https://login.microsoftonline.com/{self._settings.azure_tenant_id}"

    @property
    def issuer(self) -> str:
        return f"{self._authority}/v2.0"

    # ── step 1: send the browser to Microsoft ──────────────────────────

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._settings.azure_client_id,
                "response_type": "code",
                "redirect_uri": self._settings.azure_redirect_uri,
                "response_mode": "query",
                "scope": " ".join(DEFAULT_SCOPES),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._authority}/oauth2/v2.0/authorize?{query}"

    # ── step 2: exchange the code ──────────────────────────────────────

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        response = await self._http.post(
            f"{self._authority}/oauth2/v2.0/token",
            data={
                "client_id": self._settings.azure_client_id,
                "client_secret": self._settings.azure_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.azure_redirect_uri,
                "code_verifier": code_verifier,
                "scope": " ".join(DEFAULT_SCOPES),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            # Microsoft's error body names the tenant and app registration; useful in logs,
            # not something to hand back to a browser.
            logger.warning("oidc.token_exchange_failed", status=response.status_code)
            raise AuthenticationError("Could not complete sign-in with Microsoft.")

        payload: dict[str, Any] = response.json()
        return payload

    # ── step 3: verify the ID token ────────────────────────────────────

    async def _get_jwks(self) -> dict[str, Any]:
        if self._jwks is None:
            response = await self._http.get(f"{self._authority}/discovery/v2.0/keys")
            response.raise_for_status()
            self._jwks = response.json()
        assert self._jwks is not None
        return self._jwks

    async def verify_id_token(self, id_token: str) -> OIDCUser:
        jwks = await self._get_jwks()

        try:
            claims = jwt.decode(
                id_token,
                jwks,
                algorithms=["RS256"],
                audience=self._settings.azure_client_id,
                issuer=self.issuer,
                options={"verify_at_hash": False},
            )
        except JWTError as exc:
            # A key rotation invalidates the cache; retry once before giving up.
            logger.info("oidc.id_token_verify_retry", error=str(exc))
            self._jwks = None
            try:
                claims = jwt.decode(
                    id_token,
                    await self._get_jwks(),
                    algorithms=["RS256"],
                    audience=self._settings.azure_client_id,
                    issuer=self.issuer,
                    options={"verify_at_hash": False},
                )
            except JWTError as retry_exc:
                raise AuthenticationError(
                    "The Microsoft sign-in token is not valid."
                ) from retry_exc

        return _claims_to_user(claims)

    async def fetch_profile(self, access_token: str) -> dict[str, Any]:
        """Graph /me — fills in a display name when the ID token omits one."""
        response = await self._http.get(
            GRAPH_ME, headers={"Authorization": f"Bearer {access_token}"}
        )
        if response.status_code != 200:
            return {}
        profile: dict[str, Any] = response.json()
        return profile


def _claims_to_user(claims: dict[str, Any]) -> OIDCUser:
    object_id = claims.get("oid") or claims.get("sub")
    if not object_id:
        raise AuthenticationError("The Microsoft sign-in token identifies no user.")

    # Guests and some tenant configurations put the address in different claims.
    email = (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or ""
    ).lower()
    if not email:
        raise AuthenticationError("The Microsoft account has no email address we can use.")

    return OIDCUser(
        object_id=str(object_id),
        email=email,
        display_name=str(claims.get("name") or email.split("@")[0]),
    )


# ── PKCE helpers ───────────────────────────────────────────────────────


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)``."""
    import base64
    import hashlib

    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def generate_state() -> str:
    return secrets.token_urlsafe(32)
