"""Shared FastAPI dependencies — authentication and the ``require()`` permission guard."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.principal import Principal
from app.core.rbac import Scope, get_permission
from app.core.security import user_id_from_token
from app.services.principal_service import load_principal

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[AsyncSession, Depends(get_db)]


def _extract_token(request: Request, settings: Settings) -> str:
    """Bearer header first, session cookie second.

    The header path serves the API and tests; the cookie path serves the browser app, which
    should never hold a token in JavaScript-reachable storage.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token = header.removeprefix("Bearer ").strip()
        if token:
            return token

    cookie = request.cookies.get(settings.session_cookie_name)
    if cookie:
        return cookie

    raise AuthenticationError("No session token was supplied.")


async def get_current_principal(
    request: Request, settings: SettingsDep, session: DbDep
) -> Principal:
    token = _extract_token(request, settings)
    user_id = user_id_from_token(token, settings)

    principal = await load_principal(session, user_id)
    if principal is None:
        # The token verified, but the account is gone or deactivated. Treat as unauthenticated
        # so a deactivated user is logged straight back out rather than seeing a 403 loop.
        raise AuthenticationError("This account is no longer active.")

    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def _resolve_team_id(request: Request, param: str) -> uuid.UUID | None:
    raw = request.path_params.get(param) or request.query_params.get(param)
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def require(
    permission_key: str,
    scope: Scope = Scope.TEAM,
    *,
    team_param: str = "team_id",
) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """Build a dependency that enforces one permission.

    ``team_param`` names the path or query parameter carrying the team. When the route has
    one, the check is team-aware; when it does not, only org-wide grants can satisfy it.

    The permission key is validated against the registry *now*, at wiring time — so a typo is
    an import error at startup, not a 403 nobody can explain six months later.
    """
    get_permission(permission_key)

    async def dependency(
        request: Request, principal: CurrentPrincipal
    ) -> Principal:
        team_id = _resolve_team_id(request, team_param)

        if principal.has(permission_key, scope, team_id=team_id):
            return principal

        raise PermissionDeniedError(
            f"You do not have permission to do this ({permission_key}).",
            permission=permission_key,
            scope=scope.value,
        )

    return dependency


def require_any(
    *permission_keys: str,
    scope: Scope = Scope.TEAM,
    team_param: str = "team_id",
) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """Satisfied when the caller holds **any** of the given permissions."""
    for key in permission_keys:
        get_permission(key)

    async def dependency(
        request: Request, principal: CurrentPrincipal
    ) -> Principal:
        team_id = _resolve_team_id(request, team_param)

        if any(principal.has(key, scope, team_id=team_id) for key in permission_keys):
            return principal

        raise PermissionDeniedError(
            "You do not have permission to do this.",
            permission=" | ".join(permission_keys),
            scope=scope.value,
        )

    return dependency
