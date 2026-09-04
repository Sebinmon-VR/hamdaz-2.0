"""Entra ID token verification, exercised against a locally generated key.

This is the file that matters most. Clicking through a real Microsoft sign-in
proves the happy path once; these tests prove the *rejections* — the cases an
attacker actually produces, which a manual click-through will never show you.

We generate our own RSA keypair, serve it as a JWKS through a mock transport,
and mint tokens with it. The code under test cannot tell the difference.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.auth.oidc import AuthError, EntraOIDC, generate_pkce_pair
from app.core.config import Settings

KID = "test-key-1"
TENANT = "test-tenant"
CLIENT = "test-client"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(_key.public_key()))
    jwk.update(kid=KID, use="sig", alg="RS256")
    return {"keys": [jwk]}


def _settings() -> Settings:
    return Settings(
        azure_tenant_id=TENANT,
        azure_client_id=CLIENT,
        azure_client_secret="test-secret",
        azure_redirect_uri="http://testserver/api/v1/auth/callback",
    )


def _token(
    *,
    key: Any = None,
    kid: str = KID,
    nonce: str = "the-nonce",
    aud: str = CLIENT,
    iss: str = ISSUER,
    tid: str | None = TENANT,
    oid: str | None = "user-object-id",
    email: str | None = "Person@Hamdaz.com",
    preferred_username: str | None = None,
    name: str | None = "A Person",
    expires_in_minutes: int = 10,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": "subject",
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + timedelta(minutes=expires_in_minutes),
        "nonce": nonce,
    }
    for field, value in (
        ("tid", tid), ("oid", oid), ("email", email),
        ("preferred_username", preferred_username), ("name", name),
    ):
        if value is not None:
            claims[field] = value
    return jwt.encode(claims, key or _key, algorithm="RS256", headers={"kid": kid})


def _client(jwks: dict[str, Any] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "discovery/v2.0/keys" in str(request.url):
            return httpx.Response(200, json=jwks if jwks is not None else _jwks())
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _oidc(jwks: dict[str, Any] | None = None) -> EntraOIDC:
    return EntraOIDC(_settings(), _client(jwks))


# ── the happy path ─────────────────────────────────────────────────────


async def test_accepts_a_valid_token() -> None:
    identity = await _oidc().verify_id_token(_token(), nonce="the-nonce")
    assert identity.object_id == "user-object-id"
    assert identity.display_name == "A Person"


async def test_lowercases_email() -> None:
    """Mixed-case addresses must not create a second user on next sign-in."""
    identity = await _oidc().verify_id_token(_token(), nonce="the-nonce")
    assert identity.email == "person@hamdaz.com"


async def test_falls_back_to_preferred_username() -> None:
    """The email claim is optional on the app registration; the UPN is not."""
    token = _token(email=None, preferred_username="person@hamdaz.com")
    identity = await _oidc().verify_id_token(token, nonce="the-nonce")
    assert identity.email == "person@hamdaz.com"


async def test_display_name_falls_back_to_email_keeping_its_case() -> None:
    """No name claim: fall back to the address as written.

    The stored email is lowercased so lookups are stable, but a display name is
    read by humans, so it keeps the casing Entra sent.
    """
    identity = await _oidc().verify_id_token(_token(name=None), nonce="the-nonce")
    assert identity.display_name == "Person@Hamdaz.com"
    assert identity.email == "person@hamdaz.com"


# ── the rejections ─────────────────────────────────────────────────────


async def test_rejects_token_signed_by_another_key() -> None:
    """A correctly shaped token from a key that is not the tenant's."""
    with pytest.raises(AuthError):
        await _oidc().verify_id_token(_token(key=_other_key), nonce="the-nonce")


async def test_rejects_replayed_nonce() -> None:
    """A valid token captured elsewhere cannot be replayed into our login."""
    with pytest.raises(AuthError, match="nonce"):
        await _oidc().verify_id_token(_token(nonce="someone-elses"), nonce="the-nonce")


async def test_rejects_other_tenant() -> None:
    """Single-tenant app: another directory's user is never acceptable."""
    with pytest.raises(AuthError, match="tenant"):
        await _oidc().verify_id_token(_token(tid="some-other-tenant"), nonce="the-nonce")


async def test_rejects_wrong_audience() -> None:
    """A token minted for a different app must not work here."""
    with pytest.raises(AuthError):
        await _oidc().verify_id_token(_token(aud="another-app"), nonce="the-nonce")


async def test_rejects_wrong_issuer() -> None:
    with pytest.raises(AuthError):
        await _oidc().verify_id_token(
            _token(iss="https://login.microsoftonline.com/evil/v2.0"), nonce="the-nonce"
        )


async def test_rejects_expired_token() -> None:
    with pytest.raises(AuthError):
        await _oidc().verify_id_token(_token(expires_in_minutes=-10), nonce="the-nonce")


async def test_rejects_unknown_kid() -> None:
    with pytest.raises(AuthError, match="signing key"):
        await _oidc().verify_id_token(_token(kid="not-a-real-kid"), nonce="the-nonce")


async def test_rejects_token_without_oid() -> None:
    """Without oid there is no stable identifier to key the user row on."""
    with pytest.raises(AuthError, match="oid"):
        await _oidc().verify_id_token(_token(oid=None), nonce="the-nonce")


async def test_rejects_token_without_any_email() -> None:
    with pytest.raises(AuthError, match="email"):
        await _oidc().verify_id_token(
            _token(email=None, preferred_username=None), nonce="the-nonce"
        )


async def test_rejects_malformed_token() -> None:
    with pytest.raises(AuthError):
        await _oidc().verify_id_token("not-a-jwt", nonce="the-nonce")


# ── PKCE and the authorize URL ─────────────────────────────────────────


def test_pkce_challenge_is_s256_of_verifier() -> None:
    import base64
    import hashlib

    verifier, challenge = generate_pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in challenge  # base64url for a URL must be unpadded


def test_pkce_pair_is_unique_per_call() -> None:
    assert generate_pkce_pair()[0] != generate_pkce_pair()[0]


def test_authorization_url_carries_the_security_parameters() -> None:
    from urllib.parse import parse_qs, urlparse

    url = _oidc().authorization_url(state="ST", nonce="NO", code_challenge="CH")
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["CH"]
    assert query["state"] == ["ST"]
    assert query["nonce"] == ["NO"]
    assert query["client_id"] == [CLIENT]
    assert query["response_type"] == ["code"]
    # No Graph scopes: this flow signs people in, it does not read their data.
    assert query["scope"] == ["openid profile email"]


# ── the code exchange ──────────────────────────────────────────────────


async def test_exchange_code_sends_the_verifier_and_returns_the_id_token() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"id_token": "the-id-token"})

    oidc = EntraOIDC(_settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await oidc.exchange_code(code="the-code", code_verifier="the-verifier") == "the-id-token"
    assert seen["code_verifier"] == "the-verifier"
    assert seen["grant_type"] == "authorization_code"
    assert seen["client_secret"] == "test-secret"


async def test_exchange_code_raises_when_entra_refuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    oidc = EntraOIDC(_settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthError, match="token exchange failed"):
        await oidc.exchange_code(code="x", code_verifier="y")


async def test_exchange_code_raises_when_no_id_token_comes_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "only-this"})

    oidc = EntraOIDC(_settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthError, match="no id_token"):
        await oidc.exchange_code(code="x", code_verifier="y")
