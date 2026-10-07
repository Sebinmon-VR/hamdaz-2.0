"""Connecting an AI employee to its own Microsoft 365 account.

A super admin presses Connect on the employee and signs in **as the employee's
account** (luna@hamdaz.com, with the password IT set for it). Microsoft sends
the browser back here with a code; the code is redeemed for tokens, the
signed-in account is checked against the address on the employee — so a super
admin who signs in as themselves by mistake is told so, and their own account
is never connected — and the refresh token is kept, encrypted.

The callback carries no session of ours (it is a redirect from Microsoft), so
the request is tied to the super admin who started it by a short signed
``state`` instead.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.router import SuperAdmin
from app.core.config import get_settings
from app.core.db import get_session
from app.core.security import InvalidTokenError, sign, verify
from app.models.ai_employee import AIEmployee
from app.models.teams_chat import AIEmployeeAccount
from app.msteams.graph import EmployeeGraph, GraphAccountError, encrypt

STATE_AUDIENCE = "hamdaz:ai-employee-connect"

Session = Annotated[AsyncSession, Depends(get_session)]

admin_router = APIRouter(prefix="/assistant/admin/employees", tags=["assistant admin"])
callback_router = APIRouter(prefix="/ai-employees", tags=["ai employees in teams"])


def get_graph(request: Request) -> EmployeeGraph:
    return request.app.state.employee_graph


Graph = Annotated[EmployeeGraph, Depends(get_graph)]


class ConnectOut(BaseModel):
    url: str


@admin_router.post("/{employee_id}/connect", response_model=ConnectOut, summary="Start connecting its account")
async def connect(employee_id: uuid.UUID, admin: SuperAdmin, session: Session, graph: Graph) -> ConnectOut:
    employee = await session.get(AIEmployee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such AI employee.")
    if not employee.ms_account_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set the employee's Microsoft 365 address first (e.g. luna@hamdaz.com).",
        )
    settings = get_settings()
    state = sign(
        {"emp": str(employee.id), "by": str(admin.id)},
        secret=settings.session_secret,
        ttl_minutes=15,
        audience=STATE_AUDIENCE,
    )
    return ConnectOut(url=graph.authorize_url(state=state, login_hint=employee.ms_account_email))


@admin_router.post(
    "/{employee_id}/disconnect", status_code=status.HTTP_204_NO_CONTENT, summary="Forget its account"
)
async def disconnect(employee_id: uuid.UUID, admin: SuperAdmin, session: Session, graph: Graph) -> None:
    """Forget the sign-in; keep the record of what it did.

    The refresh token is wiped, so nothing can act as the account any more,
    but the row — and the activity log on it — stays for whoever wants to know
    what it said before it was disconnected.
    """
    account = await session.get(AIEmployeeAccount, employee_id)
    if account is not None:
        account.refresh_token_enc = ""
        account.status = "disconnected"
        account.error = None
        account.activity = [
            *(account.activity or [])[-99:],
            {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "from": admin.display_name,
                "chat": None,
                "outcome": "disconnected",
                "detail": "The account was disconnected from the admin page.",
            },
        ]
        await session.commit()
    graph.forget(str(employee_id))


def _back(**params: str) -> RedirectResponse:
    settings = get_settings()
    query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
    return RedirectResponse(
        f"{settings.frontend_url.rstrip('/')}/admin/assistant/employees?{query}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@callback_router.get("/oauth/callback", include_in_schema=False)
async def callback(request: Request, session: Session, graph: Graph) -> RedirectResponse:
    settings = get_settings()
    params = request.query_params
    if params.get("error"):
        return _back(connect_error=params.get("error_description") or params["error"])
    try:
        state = verify(params.get("state", ""), secret=settings.session_secret, audience=STATE_AUDIENCE)
    except InvalidTokenError:
        return _back(connect_error="The connection request expired. Press Connect again.")
    employee = await session.get(AIEmployee, uuid.UUID(state["emp"]))
    if employee is None:
        return _back(connect_error="That AI employee no longer exists.")
    try:
        tokens = await graph.redeem(params.get("code", ""))
        me = await graph.me(tokens["access_token"])
    except GraphAccountError as exc:
        return _back(connect_error=str(exc))

    signed_in = (me.get("mail") or me.get("userPrincipalName") or "").lower()
    expected = (employee.ms_account_email or "").lower()
    if expected and signed_in != expected and (me.get("userPrincipalName") or "").lower() != expected:
        return _back(
            connect_error=(
                f"You signed in as {signed_in}, but {employee.name}'s account is {expected}. "
                "Press Connect again and choose that account."
            )
        )
    if not tokens.get("refresh_token"):
        return _back(connect_error="Microsoft did not grant offline access; check the app's delegated permissions.")

    account = await session.get(AIEmployeeAccount, employee.id)
    if account is None:
        account = AIEmployeeAccount(employee_id=employee.id)
        session.add(account)
    account.email = signed_in or expected
    account.entra_object_id = me["id"]
    account.display_name = me.get("displayName")
    account.refresh_token_enc = encrypt(settings, tokens["refresh_token"])
    account.status = "connected"
    account.error = None
    # Nothing said before this moment is answered.
    account.connected_at = datetime.now(UTC)
    account.watermarks = {}
    account.connected_by_id = uuid.UUID(state["by"])
    graph.forget(str(employee.id))
    await session.commit()
    return _back(connected=employee.name)
