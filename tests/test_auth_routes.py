"""The HTTP surface: redirects, cookies, and who gets a 401."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import AuthError, EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign

SESSION_COOKIE = "hamdaz_session"
LOGIN_COOKIE = "hamdaz_login"


def _session_cookie(user_id, *, ttl_minutes: int = 60, secret: str | None = None) -> str:
    s = get_settings()
    return sign(
        {"sub": str(user_id)},
        secret=secret or s.session_secret,
        ttl_minutes=ttl_minutes,
        audience=SESSION_AUDIENCE,
    )


@pytest.fixture
async def signed_in_user(db):
    user = await upsert_user(
        db, EntraIdentity(object_id="oid-1", email="person@hamdaz.com", display_name="A Person")
    )
    await db.commit()
    return user


# ── /health ────────────────────────────────────────────────────────────


async def test_health_needs_no_session(client) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ── /auth/login ────────────────────────────────────────────────────────


async def test_login_redirects_to_microsoft_and_sets_state_cookie(client) -> None:
    response = await client.get("/api/v1/auth/login")
    assert response.status_code == 307
    assert "login.microsoftonline.com" in response.headers["location"]
    assert LOGIN_COOKIE in response.cookies


async def test_login_state_cookie_is_httponly(client) -> None:
    """Script must not be able to read the PKCE verifier."""
    response = await client.get("/api/v1/auth/login")
    header = response.headers["set-cookie"]
    assert "httponly" in header.lower()


async def test_login_issues_a_fresh_state_each_time(client, oidc) -> None:
    await client.get("/api/v1/auth/login")
    await client.get("/api/v1/auth/login")
    first, second = oidc.authorize_calls
    assert first["state"] != second["state"]
    assert first["nonce"] != second["nonce"]
    assert first["code_challenge"] != second["code_challenge"]


@pytest.mark.parametrize(
    "hostile",
    ["https://evil.example/steal", "//evil.example", "http://evil.example"],
)
async def test_login_neutralises_offsite_next(client, oidc, hostile: str) -> None:
    """An absolute next= must not turn our domain into an open redirect."""
    from app.auth.router import LOGIN_STATE_AUDIENCE
    from app.core.security import verify

    response = await client.get("/api/v1/auth/login", params={"next": hostile})
    state = verify(
        response.cookies[LOGIN_COOKIE],
        secret=get_settings().session_secret,
        audience=LOGIN_STATE_AUDIENCE,
    )
    assert state["next"] == "/"


async def test_login_keeps_a_relative_next(client) -> None:
    from app.auth.router import LOGIN_STATE_AUDIENCE
    from app.core.security import verify

    response = await client.get("/api/v1/auth/login", params={"next": "/proposals/42"})
    state = verify(
        response.cookies[LOGIN_COOKIE],
        secret=get_settings().session_secret,
        audience=LOGIN_STATE_AUDIENCE,
    )
    assert state["next"] == "/proposals/42"


# ── /auth/callback ─────────────────────────────────────────────────────


async def _begin_login(client, next_path: str | None = None) -> str:
    """Run the redirect leg and return the state Entra would echo back."""
    params = {"next": next_path} if next_path else {}
    response = await client.get("/api/v1/auth/login", params=params)
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


async def test_callback_completes_sign_in_and_sets_session(client, oidc) -> None:
    oidc.identity = EntraIdentity(
        object_id="oid-new", email="new@hamdaz.com", display_name="New Person"
    )
    state = await _begin_login(client)

    response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})

    assert response.status_code == 303
    assert response.headers["location"].startswith("http://frontend.test")
    assert SESSION_COOKIE in response.cookies
    # The transient cookie must not outlive the login it protected.
    assert response.cookies.get(LOGIN_COOKIE) in (None, "")


async def test_callback_honours_the_saved_next(client, oidc) -> None:
    oidc.identity = EntraIdentity(object_id="oid-1", email="a@hamdaz.com", display_name="A")
    state = await _begin_login(client, "/proposals/42")

    response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
    assert response.headers["location"] == "http://frontend.test/proposals/42"


async def test_callback_creates_the_user_row(client, oidc, db) -> None:
    from sqlalchemy import select

    from app.models import User

    oidc.identity = EntraIdentity(
        object_id="oid-fresh", email="fresh@hamdaz.com", display_name="Fresh"
    )
    state = await _begin_login(client)
    await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})

    user = await db.scalar(select(User).where(User.entra_object_id == "oid-fresh"))
    assert user is not None and user.email == "fresh@hamdaz.com"


async def test_callback_rejects_a_mismatched_state(client, oidc) -> None:
    """CSRF: the state Entra echoes must be the one we issued to this browser."""
    oidc.identity = EntraIdentity(object_id="o", email="a@hamdaz.com", display_name="A")
    await _begin_login(client)

    response = await client.get(
        "/api/v1/auth/callback", params={"code": "c", "state": "attacker-chosen"}
    )
    assert "error=state_mismatch" in response.headers["location"]
    assert SESSION_COOKIE not in response.cookies


async def test_callback_without_a_state_cookie_is_refused(client) -> None:
    response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": "s"})
    assert "error=expired_login" in response.headers["location"]
    assert SESSION_COOKIE not in response.cookies


async def test_callback_passes_through_a_cancelled_sign_in(client) -> None:
    response = await client.get("/api/v1/auth/callback", params={"error": "access_denied"})
    assert "error=access_denied" in response.headers["location"]


async def test_callback_without_a_code_is_refused(client) -> None:
    response = await client.get("/api/v1/auth/callback", params={"state": "s"})
    assert "error=missing_code" in response.headers["location"]


async def test_callback_hides_the_reason_when_entra_verification_fails(client, oidc) -> None:
    """Detail belongs in the logs, not in a URL the user can read or share."""
    oidc.error = AuthError("id_token rejected: signature verification failed")
    state = await _begin_login(client)

    response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
    location = response.headers["location"]
    assert "error=sign_in_failed" in location
    assert "signature" not in location
    assert SESSION_COOKIE not in response.cookies


async def test_deactivated_user_is_refused_at_the_callback(client, oidc, db) -> None:
    from sqlalchemy import text

    oidc.identity = EntraIdentity(
        object_id="oid-off", email="off@hamdaz.com", display_name="Off"
    )
    state = await _begin_login(client)
    await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})

    await db.execute(text("update users set is_active = false where email = 'off@hamdaz.com'"))
    await db.commit()
    client.cookies.clear()

    state = await _begin_login(client)
    response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
    assert "error=account_disabled" in response.headers["location"]


# ── /auth/me ───────────────────────────────────────────────────────────


async def test_me_requires_a_session(client) -> None:
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_me_returns_the_signed_in_user(client, signed_in_user) -> None:
    client.cookies.set(SESSION_COOKIE, _session_cookie(signed_in_user.id))
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "person@hamdaz.com"
    assert body["display_name"] == "A Person"


async def test_me_never_leaks_an_internal_field(client, signed_in_user) -> None:
    client.cookies.set(SESSION_COOKIE, _session_cookie(signed_in_user.id))
    body = (await client.get("/api/v1/auth/me")).json()
    assert set(body) == {"id", "email", "display_name", "is_active", "last_login_at"}


async def test_me_rejects_a_cookie_signed_with_another_secret(client, signed_in_user) -> None:
    client.cookies.set(
        SESSION_COOKIE, _session_cookie(signed_in_user.id, secret="attacker-guess-secret-value")
    )
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_me_rejects_an_expired_cookie(client, signed_in_user) -> None:
    client.cookies.set(SESSION_COOKIE, _session_cookie(signed_in_user.id, ttl_minutes=-5))
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_me_rejects_a_session_for_a_user_that_no_longer_exists(client) -> None:
    import uuid

    client.cookies.set(SESSION_COOKIE, _session_cookie(uuid.uuid4()))
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_me_rejects_a_non_uuid_subject(client) -> None:
    client.cookies.set(SESSION_COOKIE, _session_cookie("not-a-uuid"))
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_deactivating_a_user_ends_their_session_immediately(
    client, signed_in_user, db
) -> None:
    """The kill switch must not wait for the cookie to expire."""
    from sqlalchemy import text

    client.cookies.set(SESSION_COOKIE, _session_cookie(signed_in_user.id))
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    await db.execute(text("update users set is_active = false"))
    await db.commit()

    assert (await client.get("/api/v1/auth/me")).status_code == 401


# ── /auth/logout ───────────────────────────────────────────────────────


async def test_logout_clears_the_session_cookie(client, signed_in_user) -> None:
    """Assert the Set-Cookie header, not httpx's jar.

    httpx will not match the response's clearing cookie against one set here
    without a domain, so it stays in the jar. A browser has no such problem.
    The header is the part the backend actually controls, so that is what we
    check; the follow-up request confirms the cleared state is really rejected.
    """
    client.cookies.set(SESSION_COOKIE, _session_cookie(signed_in_user.id))

    response = await client.post("/api/v1/auth/logout")
    assert response.status_code == 204

    header = response.headers["set-cookie"]
    assert header.startswith(f'{SESSION_COOKIE}=""')
    assert "Max-Age=0" in header
    assert "httponly" in header.lower()

    client.cookies.clear()
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_logout_is_harmless_without_a_session(client) -> None:
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
