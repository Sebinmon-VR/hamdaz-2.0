"""Sign-in endpoints.

The browser never handles a token. It is redirected to Microsoft, comes back
with a code, and leaves with an HttpOnly cookie it cannot read. That is the
whole point of doing the exchange server-side: an XSS bug in the frontend
cannot walk off with a session.
"""

from __future__ import annotations

import secrets
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import SESSION_AUDIENCE, CurrentUser
from app.auth.oidc import AuthError, EntraOIDC, generate_pkce_pair
from app.auth.schemas import UserOut
from app.auth.service import upsert_user
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.security import InvalidTokenError, sign, verify

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_STATE_AUDIENCE = "hamdaz:login-state"


def get_oidc(request: Request) -> EntraOIDC:
    return request.app.state.oidc


def _safe_next(value: str | None) -> str:
    """Constrain post-login redirects to paths inside the frontend.

    Without this, ``/auth/login?next=https://evil.example`` turns our trusted
    domain into an open redirect that phishing can borrow.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _set_cookie(
    response: Response, name: str, value: str, settings: Settings, ttl_minutes: int
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=ttl_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )


def _clear_cookie(response: Response, name: str, settings: Settings) -> None:
    response.delete_cookie(
        key=name,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )


@router.get("/login", summary="Begin Microsoft sign-in")
async def login(
    settings: Annotated[Settings, Depends(get_settings)],
    oidc: Annotated[EntraOIDC, Depends(get_oidc)],
    next: Annotated[str | None, Query(description="Path to return to, must be relative")] = None,
) -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()

    response = RedirectResponse(
        oidc.authorization_url(state=state, nonce=nonce, code_challenge=challenge),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )
    # The verifier and nonce must survive the round trip to Microsoft without
    # being visible to it, so they ride in our own signed cookie.
    _set_cookie(
        response,
        settings.login_state_cookie_name,
        sign(
            {"state": state, "nonce": nonce, "verifier": verifier, "next": _safe_next(next)},
            secret=settings.session_secret,
            ttl_minutes=settings.login_state_ttl_minutes,
            audience=LOGIN_STATE_AUDIENCE,
        ),
        settings,
        settings.login_state_ttl_minutes,
    )
    return response


def _login_failed(settings: Settings, reason: str) -> RedirectResponse:
    """Send the browser back to the frontend with a short, non-leaky reason."""
    response = RedirectResponse(
        f"{settings.frontend_url}/login?{urlencode({'error': reason})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _clear_cookie(response, settings.login_state_cookie_name, settings)
    return response


@router.get("/callback", summary="Microsoft redirects here after sign-in")
async def callback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    oidc: Annotated[EntraOIDC, Depends(get_oidc)],
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    # The user cancelled, or Entra refused (no consent, blocked by CA policy).
    if error:
        return _login_failed(settings, error)
    if not code or not state:
        return _login_failed(settings, "missing_code")

    raw_state_cookie = request.cookies.get(settings.login_state_cookie_name)
    if not raw_state_cookie:
        # Usually a bookmarked callback URL or a login left open past the TTL.
        return _login_failed(settings, "expired_login")

    try:
        login_state = verify(
            raw_state_cookie, secret=settings.session_secret, audience=LOGIN_STATE_AUDIENCE
        )
    except InvalidTokenError:
        return _login_failed(settings, "expired_login")

    # CSRF check: the state Entra echoed back must match the one we issued to
    # this browser. compare_digest keeps the comparison constant-time.
    if not secrets.compare_digest(str(login_state.get("state", "")), state):
        return _login_failed(settings, "state_mismatch")

    try:
        id_token = await oidc.exchange_code(code=code, code_verifier=login_state["verifier"])
        identity = await oidc.verify_id_token(id_token, nonce=login_state["nonce"])
    except AuthError:
        # Detail goes to the logs, not the query string.
        return _login_failed(settings, "sign_in_failed")

    user = await upsert_user(session, identity)
    if not user.is_active:
        return _login_failed(settings, "account_disabled")

    response = RedirectResponse(
        f"{settings.frontend_url}{_safe_next(login_state.get('next'))}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_cookie(
        response,
        settings.session_cookie_name,
        sign(
            {"sub": str(user.id)},
            secret=settings.session_secret,
            ttl_minutes=settings.session_ttl_minutes,
            audience=SESSION_AUDIENCE,
        ),
        settings,
        settings.session_ttl_minutes,
    )
    _clear_cookie(response, settings.login_state_cookie_name, settings)
    return response


@router.get("/me", response_model=UserOut, summary="The signed-in user")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Drop the session")
async def logout(settings: Annotated[Settings, Depends(get_settings)]) -> Response:
    # Clears our cookie only. The Microsoft session stays, so the next /login is
    # silent — which is what people expect from company SSO. A full Entra sign-out
    # is a separate, deliberate action.
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_cookie(response, settings.session_cookie_name, settings)
    return response
