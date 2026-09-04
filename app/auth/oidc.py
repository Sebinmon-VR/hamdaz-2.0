"""Entra ID (Microsoft) OpenID Connect — authorization code flow with PKCE.

Microsoft answers exactly one question: *who is this person*. What they are then
allowed to do is the ERP's business, not Entra's.

The ID token is verified properly — signature against the tenant's published
JWKS, plus issuer, audience, tenant and nonce. An unverified ``jwt.decode`` here
would let anyone mint their own identity.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.core.config import Settings

#: openid = issue an ID token, profile = name, email = email claim.
#: No Graph scopes: this module signs people in, it does not read their mailbox.
SCOPES: Final = ("openid", "profile", "email")
_JWKS_TTL_SECONDS: Final = 3600
_CLOCK_SKEW_SECONDS: Final = 60


class AuthError(Exception):
    """Sign-in failed. The message is for logs, never for the browser."""


@dataclass(frozen=True, slots=True)
class EntraIdentity:
    """The claims we trust once the ID token has been verified."""

    object_id: str
    email: str
    display_name: str


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for one login attempt.

    PKCE binds the authorization code to this browser: an attacker who
    intercepts the code cannot redeem it without the verifier, which never
    leaves our cookie.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class EntraOIDC:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at = 0.0

    # ── step 1: send the browser to Microsoft ──────────────────────────

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._settings.azure_client_id,
                "response_type": "code",
                "redirect_uri": self._settings.azure_redirect_uri,
                "response_mode": "query",
                "scope": " ".join(SCOPES),
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._settings.authority}/oauth2/v2.0/authorize?{query}"

    # ── step 2: trade the code for tokens ──────────────────────────────

    async def exchange_code(self, *, code: str, code_verifier: str) -> str:
        """Redeem the authorization code and return the raw ID token."""
        response = await self._http.post(
            f"{self._settings.authority}/oauth2/v2.0/token",
            data={
                "client_id": self._settings.azure_client_id,
                "client_secret": self._settings.azure_client_secret,
                "code": code,
                "redirect_uri": self._settings.azure_redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "scope": " ".join(SCOPES),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            # Entra puts the real reason in error_description; it is the
            # difference between "wrong secret" and "redirect URI mismatch".
            raise AuthError(f"token exchange failed ({response.status_code}): {response.text}")

        id_token = response.json().get("id_token")
        if not id_token:
            raise AuthError("token response contained no id_token")
        return id_token

    # ── step 3: verify what came back ──────────────────────────────────

    async def _signing_key(self, kid: str) -> Any:
        jwks = await self._get_jwks()
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if key is None:
            # Entra rotates signing keys; a miss usually means our cache is stale.
            jwks = await self._get_jwks(force=True)
            key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if key is None:
            raise AuthError(f"no signing key matches kid {kid!r}")
        return RSAAlgorithm.from_jwk(key)

    async def _get_jwks(self, *, force: bool = False) -> dict[str, Any]:
        fresh = time.monotonic() - self._jwks_fetched_at < _JWKS_TTL_SECONDS
        if self._jwks is not None and fresh and not force:
            return self._jwks
        response = await self._http.get(self._settings.jwks_uri)
        if response.status_code != 200:
            raise AuthError(f"could not fetch JWKS ({response.status_code})")
        self._jwks = response.json()
        self._jwks_fetched_at = time.monotonic()
        return self._jwks

    async def verify_id_token(self, id_token: str, *, nonce: str) -> EntraIdentity:
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
        except jwt.PyJWTError as exc:
            raise AuthError(f"malformed id_token: {exc}") from exc
        if not kid:
            raise AuthError("id_token header has no kid")

        key = await self._signing_key(kid)
        try:
            claims = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=self._settings.azure_client_id,
                issuer=self._settings.issuer,
                leeway=_CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"id_token rejected: {exc}") from exc

        # Ties this token to the login attempt that started in *this* browser,
        # which is what stops a replayed token from elsewhere.
        if claims.get("nonce") != nonce:
            raise AuthError("id_token nonce does not match the login attempt")

        # Single-tenant app: a token from another directory is never acceptable,
        # even when correctly signed.
        if claims.get("tid") != self._settings.azure_tenant_id:
            raise AuthError(f"token issued by unexpected tenant {claims.get('tid')!r}")

        object_id = claims.get("oid")
        if not object_id:
            raise AuthError("id_token has no oid claim")

        # `email` only appears when the optional claim is configured on the app
        # registration; preferred_username carries the UPN otherwise.
        email = claims.get("email") or claims.get("preferred_username")
        if not email:
            raise AuthError("id_token carries no email or preferred_username")

        return EntraIdentity(
            object_id=object_id,
            email=email.lower(),
            display_name=claims.get("name") or email,
        )
