"""The IT Support settings API, for /admin/it-support. Super admins only:
it decides whose Teams chats an AI answers and what it may do in the ERP.

**Connect** signs the IT Support account in from the browser, so the server
that runs the agent has its sign-in (the laptop's ``python -m
app.itsupport.connect`` does the same into a local file). It returns through
the app's one registered redirect address, ``/auth/callback``, like the Mail
watch's Teams connect: the signed login-state cookie carries ``purpose:
itsupport`` and the auth router hands it back here (:func:`finish`). Only the
IT Support account is kept: signing in as anybody else keeps nothing.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.auth.oidc import generate_pkce_pair
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.mail import GRAPH_BASE
from app.core.security import sign
from app.itsupport import activity as activity_mod
from app.itsupport import features as features_mod
from app.itsupport import settings as service
from app.itsupport.graph import CONNECT_SCOPES, SCOPES, sign_in_record
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles

router = APIRouter(prefix="/it-support", tags=["it-support"])
logger = logging.getLogger("hamdaz.itsupport")

#: What the login-state cookie carries for the auth callback to hand back.
PURPOSE = "itsupport"
#: The auth router's audience for the login-state cookie (app.auth.router).
LOGIN_STATE_AUDIENCE = "hamdaz:login-state"

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can manage IT Support.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


class PersonIn(BaseModel):
    email: str = Field(max_length=320)
    name: str = Field(default="", max_length=60)


class PersonOut(PersonIn):
    #: The agent works the ERP as them; without an account it can only chat.
    has_account: bool


class ModuleOut(BaseModel):
    key: str
    name: str
    default: str
    max: str
    mode: str


class FeatureOut(BaseModel):
    key: str
    name: str
    about: str
    group: str
    default: bool
    on: bool
    #: It messages people unasked; it changes something in the ERP.
    sends: bool
    writes: bool


class ITSupportSettingsOut(BaseModel):
    enabled: bool
    people: list[PersonOut]
    show_timing: bool
    due_enabled: bool
    due_from: str
    due_until: str
    due_days: list[int]
    due_test_to: str
    brief_enabled: bool
    brief_at: str
    brain_model: str
    brain_effort: str
    modules: list[ModuleOut]
    features: list[FeatureOut] = []
    models: list[str]
    efforts: list[str]
    # the worker's heartbeat
    running: bool
    running_on: str | None
    last_seen_at: datetime | None
    signed_in_as: str | None
    last_error: str | None
    #: Whether this server itself may run it (IT_SUPPORT_CHAT_ENABLED).
    runs_here: bool
    #: The account signed in with Connect, kept for the server.
    connected: bool
    connected_account: str | None
    connected_at: datetime | None
    connected_by: str | None


class ITSupportSettingsIn(BaseModel):
    enabled: bool | None = None
    people: list[PersonIn] | None = None
    show_timing: bool | None = None
    due_enabled: bool | None = None
    due_from: str | None = None
    due_until: str | None = None
    due_days: list[int] | None = None
    due_test_to: str | None = None
    brief_enabled: bool | None = None
    brief_at: str | None = None
    brain_model: str | None = None
    brain_effort: str | None = None
    #: Each module's mode; anything left out goes back to its default.
    module_modes: dict[str, str] | None = None
    #: Switches to change, by key; the rest keep what was saved.
    features: dict[str, bool] | None = None


async def _out(session: AsyncSession, config: Settings) -> ITSupportSettingsOut:
    row = await service.get_row(session, config)
    people = row.people or []
    have = await service.accounts(session, [p["email"] for p in people])
    saved: dict[str, Any] = row.module_modes or {}
    modules = [ModuleOut(**m, mode=saved.get(m["key"], m["default"]))
               for m in service.module_rows()]
    seen = row.last_seen_at
    return ITSupportSettingsOut(
        enabled=row.enabled,
        people=[PersonOut(email=p["email"], name=p.get("name", ""),
                          has_account=p["email"] in have) for p in people],
        show_timing=row.show_timing, due_enabled=row.due_enabled, due_from=row.due_from,
        due_until=row.due_until, due_days=list(row.due_days or []),
        due_test_to=row.due_test_to, brief_enabled=row.brief_enabled,
        brief_at=row.brief_at, brain_model=row.brain_model,
        brain_effort=row.brain_effort, modules=modules,
        features=[FeatureOut(**f) for f in features_mod.rows(row.features)],
        models=list(service.MODELS),
        efforts=list(service.EFFORTS),
        running=seen is not None
        and (datetime.now(UTC) - seen).total_seconds() < service.ALIVE_SECONDS,
        running_on=row.running_on, last_seen_at=seen, signed_in_as=row.signed_in_as,
        last_error=row.last_error, runs_here=config.it_support_chat_enabled,
        connected=bool(row.sign_in),
        connected_account=(row.sign_in or {}).get("account_email"),
        connected_at=row.connected_at,
        connected_by=await session.scalar(
            select(User.display_name).where(User.id == row.connected_by_id))
        if row.connected_by_id else None,
    )


@router.get("/settings", response_model=ITSupportSettingsOut,
            summary="Who it answers, the check-ins, the model, module access, and whether it runs")
async def read_settings(admin: SuperAdmin, session: Session,
                        config: Config) -> ITSupportSettingsOut:
    out = await _out(session, config)
    await session.commit()
    return out


@router.patch("/settings", response_model=ITSupportSettingsOut, summary="Change them")
async def update_settings(body: ITSupportSettingsIn, admin: SuperAdmin, session: Session,
                          config: Config) -> ITSupportSettingsOut:
    changes = body.model_dump(exclude_unset=True)
    try:
        await service.update(session, config, actor_id=admin.id, changes=changes)
    except service.ITSupportSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await session.commit()
    return await _out(session, config)



class ActivityOut(BaseModel):
    at: datetime
    email: str
    kind: str
    summary: str
    ok: bool
    cost_usd: float | None
    detail: dict[str, Any]


class ActivityPageOut(BaseModel):
    rows: list[ActivityOut]
    #: Model spend per day, newest first: {"day": "2026-11-18", "usd": 0.42, "turns": 31}.
    spend: list[dict[str, Any]]


@router.get("/activity", response_model=ActivityPageOut,
            summary="What the agent did: changes, answers, messages sent, feedback, spend")
async def activity(admin: SuperAdmin, session: Session, kind: str | None = None,
                   email: str | None = None, limit: int = 200) -> ActivityPageOut:
    rows = await activity_mod.recent(session, kind=kind, email=email,
                                     limit=max(1, min(limit, 500)))
    return ActivityPageOut(
        rows=[ActivityOut(at=r.at, email=r.email, kind=r.kind, summary=r.summary, ok=r.ok,
                          cost_usd=float(r.cost_usd) if r.cost_usd is not None else None,
                          detail=r.detail or {}) for r in rows],
        spend=await activity_mod.spend_by_day(session, days=14))


# ── Connect: the IT Support account's sign-in, from the browser ─────────


@router.get("/connect", summary="Connect the IT Support account (opens Microsoft sign-in)")
async def connect(admin: SuperAdmin, config: Config) -> RedirectResponse:
    """Open in the browser, not from the API page: it goes to Microsoft, where
    you sign in **as IT Support** (not as yourself), and comes back to the
    settings page."""
    state = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    query = urlencode({
        "client_id": config.azure_client_id,
        "response_type": "code",
        "redirect_uri": config.azure_redirect_uri,
        "response_mode": "query",
        # The chat's scopes and the file one: consent for all at once, and the
        # one refresh token then serves both.
        "scope": CONNECT_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "login_hint": config.it_support_email,
        "prompt": "select_account",
    })
    response = RedirectResponse(f"{config.authority}/oauth2/v2.0/authorize?{query}",
                                status_code=307)
    response.set_cookie(
        key=config.login_state_cookie_name,
        value=sign({"state": state, "verifier": verifier, "purpose": PURPOSE,
                    "user_id": str(admin.id)},
                   secret=config.session_secret, ttl_minutes=config.login_state_ttl_minutes,
                   audience=LOGIN_STATE_AUDIENCE),
        max_age=config.login_state_ttl_minutes * 60, httponly=True,
        secure=config.cookie_secure, samesite=config.cookie_samesite,
        domain=config.cookie_domain, path="/",
    )
    return response


def _back(settings: Settings, **query: str) -> RedirectResponse:
    response = RedirectResponse(
        f"{settings.frontend_url}/admin/it-support?{urlencode(query)}", status_code=303)
    response.delete_cookie(
        key=settings.login_state_cookie_name, httponly=True, secure=settings.cookie_secure,
        samesite=settings.cookie_samesite, domain=settings.cookie_domain, path="/",
    )
    return response


async def finish(request: Request, session: AsyncSession, settings: Settings, *, code: str,
                 login_state: dict[str, Any]) -> RedirectResponse:
    """The code Microsoft sent back (the state already checked by the auth
    router): keep the sign-in if it is the IT Support account."""
    http: httpx.AsyncClient = request.app.state.http
    response = await http.post(f"{settings.authority}/oauth2/v2.0/token", data={
        "client_id": settings.azure_client_id,
        "client_secret": settings.azure_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.azure_redirect_uri,
        "code_verifier": login_state.get("verifier", ""),
        "scope": SCOPES,
    })
    if response.status_code != 200:
        logger.warning("it support connect: token exchange failed %s %s",
                       response.status_code, response.text[:300])
        return _back(settings, connect_error="sign_in_failed")
    tokens = response.json()
    if not tokens.get("refresh_token"):
        return _back(settings, connect_error="permission_not_granted")
    me = await http.get(
        f"{GRAPH_BASE}/me", params={"$select": "id,displayName,mail,userPrincipalName"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    account = me.json() if me.status_code == 200 else {}
    email = (account.get("mail") or account.get("userPrincipalName") or "").lower()
    if email != settings.it_support_email.lower():
        logger.warning("it support connect: signed in as %s, not %s; nothing kept",
                       email or "nobody", settings.it_support_email)
        return _back(settings, connect_error="wrong_account", account=email or "unknown")
    row = await service.get_row(session, settings)
    row.sign_in = sign_in_record(settings, tokens, account)
    row.connected_at = datetime.now(UTC)
    who = login_state.get("user_id")
    row.connected_by_id = uuid.UUID(who) if who else None
    await session.commit()
    logger.info("it support connected as %s from the settings page", email)
    return _back(settings, connected="1")


@router.delete("/connect", response_model=ITSupportSettingsOut,
               summary="Disconnect: forget the sign-in kept for the server")
async def disconnect(admin: SuperAdmin, session: Session,
                     config: Config) -> ITSupportSettingsOut:
    """The agent on the server stops within 30 seconds. A laptop's own
    sign-in (its local file) is not touched."""
    row = await service.get_row(session, config)
    row.sign_in, row.connected_at, row.connected_by_id = None, None, None
    await session.commit()
    logger.warning("it support disconnected by %s", admin.email)
    return await _out(session, config)


# ── The chats: what each person and the agent said, read-only ───────────


class ChatOut(BaseModel):
    chat_id: str
    email: str
    name: str
    #: Whether the agent answers them now (on the people list).
    answered: bool
    last_at: datetime | None


class ChatMessageOut(BaseModel):
    id: str
    at: datetime | None
    #: Sent by IT Support (the agent), not by the person.
    mine: bool
    sender: str
    text: str
    files: list[str]
    reactions: list[str]
    deleted: bool


class ChatPageOut(BaseModel):
    messages: list[ChatMessageOut]
    #: Pass back as ``more`` for the older messages; None at the chat's start.
    more: str | None


def _graph(request: Request, config: Settings):
    """The agent's own Graph client (its tokens shared), else one for this
    request: the same sign-in, from the database."""
    from app.core.db import get_session_factory
    from app.itsupport.graph import ITSupportGraph

    worker = getattr(request.app.state, "itsupport_worker", None)
    if worker is not None:
        return worker.graph
    return ITSupportGraph(config, request.app.state.http, get_session_factory())


def _when_of(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.get("/chats", response_model=list[ChatOut],
            summary="IT Support's Teams chats, one per person, newest first")
async def chats(admin: SuperAdmin, session: Session, config: Config,
                request: Request) -> list[ChatOut]:
    from app.itsupport.graph import GraphError

    row = await service.get_row(session, config)
    me = ((row.sign_in or {}).get("account_email") or config.it_support_email).lower()
    answered = {p["email"].lower() for p in row.people or []}
    names = {p["email"].lower(): p.get("name") or "" for p in row.people or []}
    await session.commit()
    try:
        found = await _graph(request, config).one_to_one_chats()
    except GraphError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"Teams couldn't be read: {exc}") from exc
    out: list[ChatOut] = []
    for chat in found:
        other = next((m for m in chat.get("members") or []
                      if (m.get("email") or "").lower() not in ("", me)), None)
        if other is None:
            continue
        email = other["email"].lower()
        out.append(ChatOut(
            chat_id=chat["id"], email=email,
            name=names.get(email) or other.get("displayName") or email,
            answered=email in answered,
            last_at=_when_of((chat.get("lastMessagePreview") or {}).get("createdDateTime"))))
    # The people it answers first, then by the latest chat.
    out.sort(key=lambda c: (not c.answered,
                            -(c.last_at.timestamp() if c.last_at else 0)))
    return out


@router.get("/chats/messages", response_model=ChatPageOut,
            summary="One chat's messages, newest first, 50 at a time")
async def chat_messages(admin: SuperAdmin, session: Session, config: Config, request: Request,
                        chat_id: str, more: str | None = None) -> ChatPageOut:
    """Read-only: what the person wrote and what the agent answered, as
    Teams has it. Tables (the team's list) come as rows of cells."""
    from app.itsupport import talk
    from app.itsupport.graph import GraphError

    row = await service.get_row(session, config)
    me = (row.sign_in or {}).get("account_id")
    await session.commit()
    graph = _graph(request, config)
    try:
        found, older = await graph.message_page(chat_id, more=more)
    except GraphError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"Teams couldn't be read: {exc}") from exc
    me = me or graph.account_id
    out: list[ChatMessageOut] = []
    for m in found:
        if m.get("messageType") != "message":
            continue
        user = (m.get("from") or {}).get("user") or {}
        deleted = bool(m.get("deletedDateTime"))
        files = [a.get("name") or "file" for a in m.get("attachments") or []
                 if a.get("contentType") not in ("messageReference",)]
        text = "" if deleted else talk.plain(m)
        if text == "(sent a file or a picture)" and files:
            text = ""
        out.append(ChatMessageOut(
            id=str(m.get("id")), at=_when_of(m.get("createdDateTime")),
            mine=bool(me) and user.get("id") == me,
            sender=user.get("displayName") or "",
            text=text, files=files,
            reactions=[r.get("reactionType") or "" for r in m.get("reactions") or []],
            deleted=deleted))
    return ChatPageOut(messages=out, more=older)
