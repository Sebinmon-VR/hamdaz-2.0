"""Sign-in and sign-out.

The browser never sees a token in JavaScript-reachable storage: the session lands in an
HttpOnly, SameSite=Lax cookie. PKCE state and verifier ride in short-lived cookies of their
own so the callback can prove the request it is completing is the one it started.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.deps import CurrentPrincipal, DbDep, SettingsDep
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.core.oidc import AzureOIDC, generate_pkce_pair, generate_state
from app.core.security import create_access_token
from app.models.identity import UserStatus
from app.models.platform import AuditAction
from app.services import audit_service
from app.services.auth_service import upsert_from_oidc

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

STATE_COOKIE = "hamdaz_oidc_state"
VERIFIER_COOKIE = "hamdaz_oidc_verifier"
_FLOW_COOKIE_TTL = 600  # ten minutes to complete a sign-in


def _cookie_kwargs(settings: Settings) -> dict[str, object]:
    return {
        "httponly": True,
        "samesite": "lax",
        # Secure requires HTTPS, which localhost does not have.
        "secure": settings.environment.value != "local",
        "path": "/",
    }


class LoginStarted(BaseModel):
    authorization_url: str


# response_model=None: the return is a union with a raw Response, which FastAPI cannot
# derive a response model from.
@router.get("/login", response_model=None)
async def login(
    settings: SettingsDep,
    response: Response,
    redirect: Annotated[bool, Query(description="Redirect instead of returning JSON")] = True,
) -> RedirectResponse | LoginStarted:
    """Begin the OIDC flow."""
    if not settings.azure_client_id or not settings.azure_tenant_id:
        raise AuthenticationError("Microsoft sign-in is not configured on this environment.")

    state = generate_state()
    verifier, challenge = generate_pkce_pair()

    oidc = AzureOIDC(settings)
    try:
        url = oidc.authorization_url(state=state, code_challenge=challenge)
    finally:
        await oidc.aclose()

    target: RedirectResponse | Response = (
        RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
        if redirect
        else response
    )
    kwargs = _cookie_kwargs(settings)
    target.set_cookie(STATE_COOKIE, state, max_age=_FLOW_COOKIE_TTL, **kwargs)  # type: ignore[arg-type]
    target.set_cookie(VERIFIER_COOKIE, verifier, max_age=_FLOW_COOKIE_TTL, **kwargs)  # type: ignore[arg-type]

    if redirect:
        assert isinstance(target, RedirectResponse)
        return target
    return LoginStarted(authorization_url=url)


class SessionOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    status: str
    #: False for a first-time user awaiting an admin. They can sign in but see nothing.
    is_active: bool
    expires_at: str


# response_model=None: this returns a redirect for browsers and JSON only when asked.
@router.get("/callback", response_model=None)
async def callback(
    request: Request,
    response: Response,
    settings: SettingsDep,
    session: DbDep,
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    state_cookie: Annotated[str | None, Cookie(alias=STATE_COOKIE)] = None,
    verifier_cookie: Annotated[str | None, Cookie(alias=VERIFIER_COOKIE)] = None,
    format: Annotated[str | None, Query(description="Set to 'json' to inspect the session")] = None,
) -> RedirectResponse | SessionOut:
    """Complete the OIDC flow and issue a session."""
    if not state_cookie or not verifier_cookie:
        raise AuthenticationError("Sign-in did not start here, or it took too long.")

    # Constant-time comparison: state is the CSRF defence for the whole flow.
    import hmac

    if not hmac.compare_digest(state, state_cookie):
        logger.warning("auth.state_mismatch")
        raise AuthenticationError("Sign-in could not be verified. Please try again.")

    oidc = AzureOIDC(settings)
    try:
        tokens = await oidc.exchange_code(code, verifier_cookie)
        id_token = tokens.get("id_token")
        if not id_token:
            raise AuthenticationError("Microsoft did not return an identity token.")

        identity = await oidc.verify_id_token(id_token)
    finally:
        await oidc.aclose()

    user, _ = await upsert_from_oidc(session, identity)

    token, expires_at = create_access_token(user_id=user.id, settings=settings)

    kwargs = _cookie_kwargs(settings)

    # A browser completing OIDC should land in the app, not on the API's JSON body. The
    # cookie is set on the redirect itself, so it survives the hop.
    target: Response = (
        response
        if format == "json"
        else RedirectResponse(
            f"{settings.frontend_url.rstrip('/')}/dashboard",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    )
    target.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.jwt_ttl_seconds,
        **kwargs,  # type: ignore[arg-type]
    )
    target.delete_cookie(STATE_COOKIE, path="/")
    target.delete_cookie(VERIFIER_COOKIE, path="/")

    if isinstance(target, RedirectResponse):
        return target

    return SessionOut(
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        status=user.status.value,
        is_active=user.status is UserStatus.ACTIVE,
        expires_at=expires_at.isoformat(),
    )


class LogoutOut(BaseModel):
    status: Literal["signed_out"]


@router.post("/logout", response_model=LogoutOut)
async def logout(
    response: Response,
    settings: SettingsDep,
    session: DbDep,
    principal: CurrentPrincipal,
) -> LogoutOut:
    await audit_service.record(
        session,
        action=AuditAction.LOGOUT,
        entity_type="user",
        entity_id=principal.user_id,
        actor=principal,
    )
    response.delete_cookie(settings.session_cookie_name, path="/")
    return LogoutOut(status="signed_out")
