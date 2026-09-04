"""The signed-cookie primitive both cookies are built on."""

from __future__ import annotations

import pytest

from app.core.security import InvalidTokenError, sign, verify

SECRET = "unit-test-secret-long-enough-for-hmac-sha256"
AUD = "test:audience"


def test_roundtrip_preserves_payload() -> None:
    token = sign({"sub": "abc"}, secret=SECRET, ttl_minutes=5, audience=AUD)
    assert verify(token, secret=SECRET, audience=AUD)["sub"] == "abc"


def test_rejects_wrong_secret() -> None:
    token = sign({"sub": "abc"}, secret=SECRET, ttl_minutes=5, audience=AUD)
    with pytest.raises(InvalidTokenError):
        verify(token, secret="a-different-secret-entirely", audience=AUD)


def test_rejects_wrong_audience() -> None:
    """A login-state cookie must not be usable as a session cookie."""
    token = sign({"sub": "abc"}, secret=SECRET, ttl_minutes=5, audience="hamdaz:login-state")
    with pytest.raises(InvalidTokenError):
        verify(token, secret=SECRET, audience="hamdaz:session")


def test_rejects_expired() -> None:
    token = sign({"sub": "abc"}, secret=SECRET, ttl_minutes=-1, audience=AUD)
    with pytest.raises(InvalidTokenError):
        verify(token, secret=SECRET, audience=AUD)


def test_rejects_tampered_payload() -> None:
    """The attack that matters: swap the subject, keep the signature."""
    import base64
    import json

    token = sign({"sub": "user-a"}, secret=SECRET, ttl_minutes=5, audience=AUD)
    header, payload, signature = token.split(".")

    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    claims = json.loads(raw)
    claims["sub"] = "user-b"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")

    with pytest.raises(InvalidTokenError):
        verify(f"{header}.{forged}.{signature}", secret=SECRET, audience=AUD)


def test_rejects_alg_none() -> None:
    """Classic JWT downgrade: claim there is no signature and supply none."""
    import base64
    import json

    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'user-b', 'aud': AUD})}."
    with pytest.raises(InvalidTokenError):
        verify(forged, secret=SECRET, audience=AUD)


def test_requires_expiry_claim() -> None:
    """A token with no exp would never age out; it must be refused."""
    import jwt

    forged = jwt.encode({"sub": "x", "aud": AUD}, SECRET, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        verify(forged, secret=SECRET, audience=AUD)
