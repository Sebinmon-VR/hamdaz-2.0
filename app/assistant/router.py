"""The assistant's HTTP surface: a chat for everyone, a control panel for one role.

Two routers in one file, and the split between them is the point:

* the **chat** routes are open to any signed-in user, and refuse on their own
  terms — switched off, not released, over a cap — rather than with a bare 403
  that says nothing
* the **admin** routes are super admin only. Not ``ADMIN_ROLES``: deciding what
  an assistant may do on everyone's behalf, and reading every conversation's
  cost, is a narrower question than running teams. A CEO or manager gets 403
  here, exactly as they do on team module visibility.

Sending a message answers with Server-Sent Events rather than JSON. A turn can
take a while and calls tools as it goes, and somebody watching a spinner with
no idea whether anything is happening is the difference between an assistant
people use and one they abandon.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import service as access_service
from app.assistant import service
from app.assistant.agent import Assistant, TurnContext, instructions_for
from app.assistant.cache import ActorCache, ConfigCache, PlacesCache
from app.assistant.catalogue import (
    DEFAULT_REALTIME_MODEL,
    DEFAULT_SPEECH_MODEL,
    DEFAULT_VOICE,
    GROUPS_BY_KEY,
    REALTIME_INSTRUCTIONS,
    REALTIME_MODELS,
    REALTIME_TOKEN_SECONDS,
    SPEECH_MODELS,
    TOOLS_BY_KEY,
    VOICE_INSTRUCTIONS,
    VOICE_MAX_CHARS,
    VOICES,
)
from app.assistant.llm import LLMError
from app.assistant.executor import ToolExecutor
from app.assistant.places import (
    PlaceError,
    describe,
    detail_page,
    fill,
    places_from,
    resolve,
)
from app.assistant.records import (
    FINDERS,
    RecordError,
    label_of,
    looks_like_id,
    match,
    rows_of,
)
from app.assistant.policy import resolve_tools
from app.assistant.schemas import (
    PlaceOut,
    RealtimeStartIn,
    AccessRuleIn,
    AccessRuleOut,
    AccessRulePatch,
    AnalyticsOut,
    ConfirmIn,
    ConversationDetailOut,
    ConversationIn,
    ConversationOut,
    MessageOut,
    ModelIn,
    ModelOut,
    ModuleCapabilityOut,
    ModulePolicyIn,
    ModulePolicyOut,
    PendingActionOut,
    PendingOut,
    RealtimeCallIn,
    RealtimeCallOut,
    RealtimeSessionOut,
    RealtimeToolOut,
    RealtimeUsageIn,
    RunDetailOut,
    RunOut,
    RunPage,
    SendIn,
    SettingsIn,
    SettingsOut,
    SpeakIn,
    StatusOut,
    ToolCapabilityOut,
    ToolPolicyIn,
    VoiceModelIn,
    VoiceModelOut,
    VoiceOptionsOut,
    VoiceOut,
)
from app.assistant.service import (
    AssistantConflictError,
    AssistantError,
    AssistantNotFoundError,
)
from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.models.assistant import AssistantRun, EventKind, RunStatus
from app.models.team import Team, slugify
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles
from app.roles.service import global_role_keys

router = APIRouter(prefix="/assistant", tags=["assistant"])
admin_router = APIRouter(prefix="/assistant/admin", tags=["assistant admin"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


def _translate(exc: AssistantError) -> HTTPException:
    if isinstance(exc, AssistantNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, AssistantConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    """Only a super admin configures the assistant or reads everyone's runs."""
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only a super admin can configure the assistant. This is deliberately "
                "narrower than the admin role used elsewhere."
            ),
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


def get_assistant(request: Request) -> Assistant:
    agent = getattr(request.app.state, "assistant", None)
    if agent is None:  # pragma: no cover - only if the lifespan did not run
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The assistant is not running.",
        )
    return agent


Agent = Annotated[Assistant, Depends(get_assistant)]


def get_places_cache(request: Request) -> PlacesCache:
    return request.app.state.assistant_places


def get_config_cache(request: Request) -> ConfigCache:
    cache = getattr(request.app.state, "assistant_config", None)
    return cache if cache is not None else ConfigCache()


def get_actor_cache(request: Request) -> ActorCache:
    cache = getattr(request.app.state, "assistant_actors", None)
    return cache if cache is not None else ActorCache()


ConfigCached = Annotated[ConfigCache, Depends(get_config_cache)]
ActorCached = Annotated[ActorCache, Depends(get_actor_cache)]


async def cached_snapshot(session: AsyncSession, cache: ConfigCache) -> service.Snapshot:
    """The assistant's configuration, from memory when it is fresh."""
    hit = cache.get()
    if hit is not None:
        return hit
    return cache.put(await service.load_snapshot(session))


async def cached_actor(session: AsyncSession, cache: ActorCache, user: User):
    """What this person can see. Stale only in what is *offered* — see cache.py."""
    hit = cache.get(user.id)
    if hit is not None:
        return hit
    return cache.put(user.id, await service.build_actor(session, user))


def _forget(request: Request) -> None:
    """Drop the configuration cache after an administrator changes it."""
    cache = getattr(request.app.state, "assistant_config", None)
    if cache is not None:
        cache.invalidate()



def _session_cookie(request: Request, settings: Settings) -> str:
    """The caller's own session token, which every tool call is made with.

    It is present by definition — ``current_user`` already accepted it — but a
    machine caller using the API key header would have none, and an assistant
    turn with no session to act as must not start.
    """
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "The assistant acts as the signed-in person and needs their session "
                "cookie. Sign in through the browser rather than calling with a key."
            ),
        )
    return token


# ── the chat ───────────────────────────────────────────────────────────


@admin_router.get("", include_in_schema=False)
async def _admin_root() -> dict[str, str]:  # pragma: no cover - convenience only
    return {"see": "/assistant/admin/settings"}


@router.get(
    "/open",
    response_model=PlaceOut,
    summary="Where a page lives",
)
async def open_place(
    page: Annotated[str, Query(min_length=1, max_length=80)],
    request: Request,
    user: CurrentUser,
    session: Session,
    team: Annotated[str | None, Query(max_length=80)] = None,
    record: Annotated[str | None, Query(max_length=128)] = None,
) -> PlaceOut:
    """Resolve a page name to a route the browser can go to.

    **This changes nothing.** It is a lookup, and the app on the other end is
    what actually moves — which is the honest shape for it: the assistant does
    not have hands, it has an opinion about where the person wants to be, and
    the screen decides whether to act on it.

    What it does do is make that opinion *checkable*. The destinations are the
    caller's own effective access, so a page they may not reach cannot be
    resolved at all, and the refusal says what they can reach instead rather
    than leaving the model to guess again.
    """
    places = await _places_for(request, session, user)
    opened: str | None = None
    try:
        place = resolve(places, page)
        # A record was named but the page resolved to a list — "open the Hamdaz
        # ERP project" arrives as page='projects' with the name on it, which is
        # how a model phrases it every time. Opening the list would answer half
        # the request and look like success.
        if record and not any(param != "slug" for param in place.params):
            detail = detail_page(places, place.module_key)
            if detail is not None:
                place = detail
        # A `record` that is not an id is a name, and naming a thing is how
        # people ask for it. Looked up here rather than left to the model: an
        # instruction to search first can be skipped, and when it is skipped
        # the failure is a list opened while claiming a record was — which
        # reads as success. See app/assistant/records.py.
        if record and not looks_like_id(record) and place.params:
            record, opened = await _find_record(
                request, place.module_key, record, session_cookie=_session_cookie(
                    request, get_settings()
                )
            )
        path = fill(place, team=team, record=record)
    except RecordError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except PlaceError as exc:
        # "Take me to presales" names a team, not a page, and there is no way
        # for the model to know which of the two it just said. Rather than
        # refusing something a person would call obvious, a name that turns out
        # to be a team opens that team.
        fallback = await _team_page(session, places, page)
        if fallback is None:
            # 404 rather than 422: the model asked for somewhere that is not
            # there for this person, and the message is the correction it needs.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        place, path = fallback

    return PlaceOut(
        page=place.key,
        module=place.module_key,
        name=place.name,
        # What was actually opened, by the name the record carries — so the
        # assistant can say "Opened Hamdaz ERP" and be describing the thing on
        # the screen rather than the page it lives on.
        label=f"{place.module_name} · {opened}" if opened else place.label,
        path=path,
    )


async def _find_record(
    request: Request, module_key: str, wanted: str, *, session_cookie: str
) -> tuple[str, str]:
    """The id of the record somebody named, and the name it actually has.

    Through the module's own list route, in-process, carrying the caller's
    session — so what can be found is exactly what they could see on the
    screen, decided by the route rather than by a second copy of its rules.
    """
    finder = FINDERS.get(module_key)
    if finder is None:
        raise RecordError(
            f"A {module_key} record has to be opened by id. Find it with the "
            f"{module_key} tools first, then call this again with that id."
        )

    executor: ToolExecutor = request.app.state.assistant_executor
    outcome = await executor.call(
        SimpleNamespace(
            method="GET",
            path=finder.path,
            key=f"{module_key}.__find__",
            split=lambda arguments: ({}, dict(finder.params), None),
        ),
        {},
        session_cookie=session_cookie,
    )
    if not outcome.ok:
        raise RecordError(
            f"Could not look up {module_key} records right now ({outcome.status})."
        )

    row = match(finder, rows_of(finder, outcome.body), wanted)
    found = row.get("id")
    if not found:
        raise RecordError(f"That {module_key} record has no id to open.")
    return str(found), label_of(finder, row)


async def _team_page(
    session: AsyncSession, places: list[Any], wanted: str
) -> tuple[Any, str] | None:
    """That team's page, when the name turns out to be a team.

    Deliberately a *fallback* rather than a first attempt: a team called
    Reports must not shadow the reports module. And deliberately by handle
    only — matching on display names would make "the finance team" and the
    finance module race each other, which is the kind of ambiguity that is
    only noticed when somebody lands somewhere strange.
    """
    handle = slugify(wanted)
    if not handle:
        return None
    team = await session.scalar(select(Team).where(Team.slug == handle))
    if team is None or team.archived_at is not None:
        return None
    for place in places:
        if place.key == "teams.detail":
            try:
                return place, fill(place, team=team.slug)
            except PlaceError:
                return None
    return None


@router.get("/status", response_model=StatusOut, summary="May I use the assistant, and for what")
async def status_for_me(
    user: CurrentUser,
    session: Session,
    settings: Config,
    config_cache: ConfigCached,
    actor_cache: ActorCached,
) -> StatusOut:
    """What the frontend asks before showing the chat.

    Answers three questions at once: is it on, am I allowed, and what can it do
    for me — the last being the tool list this person would actually get, which
    is what drives the suggestion chips.
    """
    snapshot = await cached_snapshot(session, config_cache)
    actor = await cached_actor(session, actor_cache, user)
    admission = await service.admission_for(session, snapshot, actor)

    modules: list[ModuleCapabilityOut] = []
    if admission.admitted:
        grouped: dict[str, list[ToolCapabilityOut]] = {}
        for tool in resolve_tools(
            snapshot.settings, snapshot.module_policies, snapshot.tool_policies, actor
        ):
            grouped.setdefault(tool.spec.module_key, []).append(
                ToolCapabilityOut(
                    key=tool.spec.key,
                    label=tool.spec.label,
                    kind=tool.spec.kind,
                    requires_confirmation=tool.requires_confirmation,
                )
            )
        modules = [
            ModuleCapabilityOut(key=key, name=GROUPS_BY_KEY[key].name, tools=tools)
            for key, tools in grouped.items()
        ]

    return StatusOut(
        enabled=snapshot.settings.enabled,
        admitted=admission.admitted,
        code=admission.code,
        reason=admission.reason,
        model=snapshot.settings.model_key if admission.admitted else None,
        voice_enabled=snapshot.settings.voice_enabled,
        voice=snapshot.settings.voice if snapshot.settings.voice_enabled else None,
        realtime_enabled=snapshot.settings.realtime_enabled and admission.admitted,
        modules=modules,
    )


@router.post(
    "/conversations",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a chat",
)
async def create_conversation(
    body: ConversationIn,
    user: CurrentUser,
    session: Session,
    config_cache: ConfigCached,
    actor_cache: ActorCached,
) -> ConversationOut:
    snapshot = await cached_snapshot(session, config_cache)
    actor = await cached_actor(session, actor_cache, user)
    admission = await service.admission_for(session, snapshot, actor)
    if not admission.admitted:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=admission.reason)
    conversation = await service.create_conversation(session, user=user, title=body.title)
    return ConversationOut.model_validate(conversation)


@router.get("/conversations", response_model=list[ConversationOut], summary="My chats")
async def my_conversations(
    user: CurrentUser,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ConversationOut]:
    rows = await service.list_conversations(session, user.id, limit=limit)
    return [ConversationOut.model_validate(c) for c in rows]


def _pending_out(run: AssistantRun | None) -> PendingOut | None:
    if run is None or run.status != RunStatus.AWAITING_CONFIRMATION or not run.pending:
        return None
    return PendingOut(
        run_id=run.id,
        actions=[PendingActionOut(**action) for action in run.pending],
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailOut,
    summary="One chat, with its messages",
)
async def read_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, session: Session
) -> ConversationDetailOut:
    try:
        conversation = await service.get_conversation(session, conversation_id, user_id=user.id)
    except AssistantError as exc:
        raise _translate(exc) from exc
    messages = await service.list_messages(session, conversation.id)
    open_run = await service.open_run_for(session, conversation.id)
    return ConversationDetailOut(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        last_message_at=conversation.last_message_at,
        messages=[MessageOut.model_validate(m) for m in messages],
        pending=_pending_out(open_run),
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a chat",
)
async def delete_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, session: Session
) -> None:
    try:
        conversation = await service.get_conversation(session, conversation_id, user_id=user.id)
    except AssistantError as exc:
        raise _translate(exc) from exc
    await service.delete_conversation(session, conversation)


def _sse_response(stream: Any) -> StreamingResponse:
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Nginx and the Azure front end buffer by default, which holds every
            # event until the turn ends and defeats the point of streaming.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _prepare(
    request: Request,
    user: User,
    session: AsyncSession,
    settings: Settings,
    conversation: Any | None = None,
    page: str | None = None,
) -> tuple[service.Snapshot, TurnContext]:
    """Everything a turn needs, with every gate checked before anything starts."""
    snapshot = await cached_snapshot(session, get_config_cache(request))
    actor = await cached_actor(session, get_actor_cache(request), user)
    admission = await service.admission_for(session, snapshot, actor)
    if not admission.admitted:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=admission.reason)
    if snapshot.model is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The assistant is set to use {snapshot.settings.model_key!r}, which is "
                "not in its model list. A super admin needs to pick another."
            ),
        )
    if not settings.openai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The assistant has no OpenAI API key configured. Ask a super admin "
                "to set OPENAI_API_KEY."
            ),
        )
    tools = resolve_tools(
        snapshot.settings, snapshot.module_policies, snapshot.tool_policies, actor
    )
    context = TurnContext(
        user=user,
        actor=actor,
        session_cookie=_session_cookie(request, settings),
        snapshot=snapshot,
        tools=tools,
        subject=_subject_note(conversation),
        where=await _where(request, session, user, page),
    )
    return snapshot, context


async def _where(
    request: Request, session: AsyncSession, user: User, page: str | None
) -> str | None:
    """The screen the person is on, said so the model can act on it.

    Turned from a route back into a screen — and, when the route is about one
    record, that record's id — because "open this one" and "who filed it" are
    only answerable if the assistant knows what "this" is. Resolved against the
    same list that decides where it can send somebody, so a path that is not
    one of this app's own screens simply says nothing rather than being
    repeated back as fact.
    """
    if not page or not page.startswith("/"):
        return None
    places = await _places_for(request, session, user)
    place = describe(places, page)
    if place is None:
        return None

    nl = chr(10)
    told = f"- Screen: {place.label} ({place.key}){nl}- Route: {page}"
    # The parameter a detail route carries IS the record they are looking at,
    # which is the single most useful fact here: it turns "summarise this
    # report" into a tool call rather than a question.
    pattern = [part for part in place.path.split("/") if part]
    actual = [part for part in page.split("/") if part]
    for expected, value in zip(pattern, actual):
        if expected == "[slug]":
            told += f"{nl}- Team on this screen: {value}"
        elif expected.startswith("["):
            told += f"{nl}- Record open on this screen: {value}"

    # What else is in this module, by key. Without it the assistant knows
    # a person is on a project and not that "projects.plan" is a thing it
    # could open — so "show me its plan" became another view of the page
    # they were already looking at.
    siblings = sorted(
        other.key for other in places if other.module_key == place.module_key
    )
    if len(siblings) > 1:
        told += f"{nl}- Other screens here: " + ", ".join(siblings)

    # And the tool that returns what is actually ON the screen. The model
    # has a hundred tools and no way to know which one backs the page in
    # front of somebody — so "what is on here" and "summarise this" were
    # being answered from the route name alone. Named from the catalogue,
    # so a module whose tools change says something true either way.
    reader = None
    if any(part.startswith("[") and part != "[slug]" for part in pattern):
        if f"{place.module_key}.get" in TOOLS_BY_KEY:
            reader = f"{place.module_key}.get, with the record id above"
    for candidate in (f"{place.module_key}.list", f"{place.module_key}.mine"):
        if reader is None and candidate in TOOLS_BY_KEY:
            reader = candidate
    if reader:
        told += f"{nl}- The data on this screen comes from: {reader}. Read it before answering about what they can see."
    return told


async def _places_for(request: Request, session: AsyncSession, user: User) -> list[Any]:
    """This person's reachable screens, from the cache when it is warm."""
    cached = get_places_cache(request).get(user.id)
    if cached is not None:
        return cached
    access = await access_service.effective_access(
        session, user_id=user.id, global_roles=await global_role_keys(session, user.id)
    )
    return get_places_cache(request).put(user.id, places_from(access))


def _subject_note(conversation: Any | None) -> str | None:
    """One paragraph telling the model what this chat is about.

    Only for a chat opened from somewhere specific. A report page's box passes
    its report, so "why is that blocked?" means the right thing without a tool
    round spent finding out which report the person is looking at. The id is
    included because the way to learn more about it is ``reports.get``, and the
    model cannot call that with a report it was never told the id of.
    """
    if conversation is None or not conversation.subject_kind:
        return None
    if conversation.subject_kind == "report":
        label = conversation.subject_label or "a team report"
        return (
            f"This chat was opened from a team report: {label}. "
            f"Its report id is {conversation.subject_id}. "
            "The summary already in this chat was written from that report. For "
            "anything it does not cover, call reports.get with that id rather "
            "than guessing, and answer from what the report actually says — "
            "including saying when it does not say."
        )
    return f"This chat was opened from {conversation.subject_label or conversation.subject_kind}."


@router.post(
    "/conversations/{conversation_id}/messages",
    summary="Send a message and stream the answer",
    response_class=StreamingResponse,
)
async def send_message(
    conversation_id: uuid.UUID,
    body: SendIn,
    request: Request,
    user: CurrentUser,
    session: Session,
    settings: Config,
    agent: Agent,
) -> StreamingResponse:
    """Ask the assistant something.

    The response is an event stream: ``run`` once at the start, ``text`` deltas
    as the answer is written, ``tool_call`` and ``tool_result`` around each
    tool, ``confirm`` if a write is waiting on the person, ``error`` if
    something went wrong, and ``done`` last with the final status and cost.
    """
    try:
        conversation = await service.get_conversation(session, conversation_id, user_id=user.id)
    except AssistantError as exc:
        raise _translate(exc) from exc

    open_run = await service.open_run_for(session, conversation.id)
    if open_run is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This chat is still working on the previous message."
                if open_run.status == RunStatus.RUNNING
                else "This chat is waiting for you to confirm an action first."
            ),
        )

    snapshot, context = await _prepare(
        request, user, session, settings, conversation, page=body.page
    )
    run = await service.create_run(
        session,
        conversation=conversation,
        user=user,
        settings=snapshot.settings,
        user_text=body.text,
    )
    await service.add_message(
        session, conversation, role="user", content=body.text, run_id=run.id
    )
    await service.add_event(
        session, run, "user_message", payload={"chars": len(body.text)}
    )
    # Committed before the turn starts: the background task opens its own
    # sessions, and it must be able to see the run it was handed.
    await session.commit()

    return _sse_response(agent.start(run.id, context))


@router.post(
    "/conversations/{conversation_id}/confirm",
    summary="Approve or decline a waiting action",
    response_class=StreamingResponse,
)
async def confirm(
    conversation_id: uuid.UUID,
    body: ConfirmIn,
    request: Request,
    user: CurrentUser,
    session: Session,
    settings: Config,
    agent: Agent,
) -> StreamingResponse:
    """Answer the confirmation card. The turn picks up where it paused.

    Declining is not an error: the model is told the person said no, and gets
    to respond to that, which is why this streams like a message does.
    """
    try:
        conversation = await service.get_conversation(session, conversation_id, user_id=user.id)
        run = await service.get_run(session, body.run_id)
    except AssistantError as exc:
        raise _translate(exc) from exc

    if run.conversation_id != conversation.id or run.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run")
    if run.status != RunStatus.AWAITING_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"That run is not waiting for confirmation ({run.status}).",
        )

    _, context = await _prepare(request, user, session, settings, conversation)
    await session.commit()
    return _sse_response(agent.resume(run.id, context, approved=body.approved))


# ── the voice ──────────────────────────────────────────────────────────


def _voice_settings(row) -> tuple[str, str, str]:
    """(model, voice, instructions) with the shipped wording filled in."""
    return (
        row.voice_model or DEFAULT_SPEECH_MODEL,
        row.voice or DEFAULT_VOICE,
        (row.voice_instructions or VOICE_INSTRUCTIONS).strip(),
    )


@router.get("/voices", response_model=VoiceOptionsOut, summary="Voices to choose from")
async def voices(user: CurrentUser, session: Session) -> VoiceOptionsOut:
    """Every voice, and which one is configured.

    Open to any signed-in user rather than to admins alone: an admin screen
    needs it to offer samples, and the chat page needs to know whether to show
    a speaker button at all.
    """
    settings = await service.get_settings(session)
    model, voice, instructions = _voice_settings(settings)
    return VoiceOptionsOut(
        enabled=settings.voice_enabled,
        model=model,
        voice=voice,
        instructions=instructions,
        max_chars=VOICE_MAX_CHARS,
        voices=[VoiceOut(key=key, active=key == voice) for key in VOICES],
        speech_models=list(SPEECH_MODELS),
        realtime_models=list(REALTIME_MODELS),
        realtime_model=settings.realtime_model or DEFAULT_REALTIME_MODEL,
    )


@router.post("/speech", summary="Read text aloud", response_class=StreamingResponse)
async def speech(
    body: SpeakIn,
    request: Request,
    user: CurrentUser,
    session: Session,
    config: Config,
    agent: Agent,
) -> StreamingResponse:
    """Turn an answer into audio, streamed as it is generated.

    Streamed rather than returned whole so the browser can start playing on the
    first chunk; waiting for a whole clip before any sound feels broken even
    when it is quick.

    Gated by the same admission the chat uses, so somebody the assistant has not
    been released to cannot use this as a text-to-speech service. Choosing a
    different ``voice`` is allowed only for a super admin sampling them — for
    everybody else the configured voice is the one they get.
    """
    snapshot = await cached_snapshot(session, get_config_cache(request))
    actor = await cached_actor(session, get_actor_cache(request), user)
    admission = await service.admission_for(session, snapshot, actor)
    if not admission.admitted:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=admission.reason)
    if not snapshot.settings.voice_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The assistant's voice is switched off. A super admin can turn it on.",
        )
    if not config.openai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The assistant has no OpenAI API key configured.",
        )

    model, voice, instructions = _voice_settings(snapshot.settings)
    if body.voice and body.voice != voice:
        if not actor.is_super_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only a super admin can pick a different voice.",
            )
        if body.voice not in VOICES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"voice must be one of: {', '.join(VOICES)}",
            )
        voice = body.voice

    # Billed before a byte is sent, and that is not an oversight. OpenAI reports
    # nothing back on a speech call, the audio streams straight past us to the
    # browser, and a listener who closes the tab mid-clip has still been charged
    # for the whole of it. The text we are about to send is therefore the last
    # moment the figure is both knowable and right.
    await service.record_speech_usage(
        session,
        user_id=user.id,
        model_key=model,
        voice=voice,
        characters=len(body.text),
    )
    await session.commit()

    audio = agent.speak(body.text, model=model, voice=voice, instructions=instructions)
    return StreamingResponse(
        audio,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            # Same reason as the chat stream: a buffering proxy would hold every
            # chunk until the clip finished and undo the point of streaming.
            "X-Accel-Buffering": "no",
        },
    )


# ── spoken conversation ────────────────────────────────────────────────
#
# OpenAI runs the loop here, not us: the browser streams microphone audio to
# them and hears speech back. Two things keep that inside the same access model
# as the text chat.
#
# The session is **defined server-side**. Model, instructions, voice and the
# tool list are all fixed when the token is minted, so a tampered client cannot
# give itself a tool this person may not use.
#
# Tools **do not run in the browser**. Every call comes back to the proxy below,
# which resolves this person's policy again and executes through the same route
# as the text chat, with their own session cookie. Nothing here is trusted
# because the client said it.


@router.post(
    "/realtime/session",
    response_model=RealtimeSessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Open a spoken conversation",
)
async def realtime_session(
    request: Request,
    user: CurrentUser,
    session: Session,
    config: Config,
    agent: Agent,
    body: RealtimeStartIn | None = None,
) -> RealtimeSessionOut:
    """Mint a short-lived token for one spoken conversation.

    The token is worth two minutes and opens exactly the session described here.
    A run is created alongside it so the conversation has somewhere to be
    recorded; unlike a typed turn, its cost is not known to us, because OpenAI
    bills the realtime session directly and does not report usage back.
    """
    snapshot = await cached_snapshot(session, get_config_cache(request))
    actor = await cached_actor(session, get_actor_cache(request), user)
    admission = await service.admission_for(session, snapshot, actor)
    if not admission.admitted:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=admission.reason)
    if not snapshot.settings.realtime_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Spoken conversation is switched off. A super admin can turn it on "
                "in the assistant settings."
            ),
        )
    if not config.openai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The assistant has no OpenAI API key configured.",
        )

    tools = resolve_tools(
        snapshot.settings, snapshot.module_policies, snapshot.tool_policies, actor
    )
    # Writes are held back unless a super admin has accepted that voice mode
    # confirms differently — see the note at the top of this section.
    if not snapshot.settings.realtime_writes_enabled:
        tools = [t for t in tools if not t.spec.is_write]

    # Where they are, at the moment the token is minted. A spoken session's
    # instructions are fixed for its lifetime — the browser is never allowed
    # to rewrite them, which is what stops a tampered client granting itself
    # a tool — so this is the only chance to say it, and a conversation that
    # outlives a navigation will still be describing where it began.
    spoken = (chr(10) * 2).join(
        [
            instructions_for(
                snapshot.settings,
                actor,
                user,
                tools,
                where=await _where(
                    request, session, user, body.page if body else None
                ),
            ),
            REALTIME_INSTRUCTIONS,
        ]
    )
    model = snapshot.settings.realtime_model or DEFAULT_REALTIME_MODEL
    voice = snapshot.settings.voice or DEFAULT_VOICE

    conversation = await service.create_conversation(
        session, user=user, title="Spoken conversation"
    )
    run = await service.create_run(
        session,
        conversation=conversation,
        user=user,
        settings=snapshot.settings,
        user_text="(spoken conversation)",
    )
    run.model_key = model
    await service.add_event(
        session, run, EventKind.USER_MESSAGE, payload={"realtime": True, "model": model}
    )
    await session.commit()

    try:
        secret, expires_at = await agent.realtime_secret(
            model=model,
            instructions=spoken,
            # Realtime has no tool search, so everything it may use is
            # loaded up front. That is another reason writes stay off
            # there by default: the list is longer than it looks.
            tools=[t.spec.realtime_definition() for t in tools],
            voice=voice,
            seconds=REALTIME_TOKEN_SECONDS,
        )
    except LLMError as exc:
        run.status = RunStatus.FAILED
        run.error = str(exc)
        run.finished_at = datetime.now(UTC)
        await service.add_event(session, run, EventKind.ERROR, payload={"message": str(exc)})
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    return RealtimeSessionOut(
        client_secret=secret,
        expires_at=expires_at,
        model=model,
        voice=voice,
        run_id=run.id,
        writes_enabled=snapshot.settings.realtime_writes_enabled,
        tools=[
            RealtimeToolOut(
                name=t.spec.name,
                tool_key=t.spec.key,
                label=t.spec.label,
                kind=t.spec.kind,
                requires_confirmation=t.requires_confirmation,
                warning=t.spec.warning,
            )
            for t in tools
        ],
    )


@router.post(
    "/realtime/call",
    response_model=RealtimeCallOut,
    summary="Run one tool the spoken assistant asked for",
)
async def realtime_call(
    body: RealtimeCallIn,
    request: Request,
    user: CurrentUser,
    session: Session,
    settings: Config,
    agent: Agent,
) -> RealtimeCallOut:
    """Execute a tool call from a spoken conversation, and record it.

    The client relays what the model asked for; it does not decide what may run.
    This resolves the person's tools again from policy, so a name that is not on
    their list is refused however convincingly it was asked for, and the call
    itself travels the same route as the text chat with their own session.

    A write that needs confirming is refused the first time with
    ``requires_confirmation``. The client asks aloud and calls again with
    ``confirmed``. That is a weaker guarantee than the text chat, where the
    server parks the run and nothing can run until a person answers — which is
    exactly why ``realtime_writes_enabled`` is its own switch.
    """
    try:
        run = await service.get_run(session, body.run_id)
    except AssistantError as exc:
        raise _translate(exc) from exc
    if run.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run")

    snapshot = await cached_snapshot(session, get_config_cache(request))
    actor = await cached_actor(session, get_actor_cache(request), user)
    admission = await service.admission_for(session, snapshot, actor)
    if not admission.admitted:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=admission.reason)

    allowed = {
        t.spec.name: t
        for t in resolve_tools(
            snapshot.settings, snapshot.module_policies, snapshot.tool_policies, actor
        )
    }
    resolved = allowed.get(body.name) or allowed.get(body.name.replace(".", "__"))
    if resolved is None or (
        resolved.spec.is_write and not snapshot.settings.realtime_writes_enabled
    ):
        await service.add_event(
            session,
            run,
            EventKind.BLOCKED_BY_POLICY,
            tool_key=body.name,
            payload={"reason": "not available in a spoken conversation"},
        )
        await session.commit()
        return RealtimeCallOut(
            ok=False,
            status=403,
            output=json.dumps(
                {"error": "That is not something I can do here.", "status": 403}
            ),
        )

    spec = resolved.spec
    if resolved.requires_confirmation and not body.confirmed:
        await service.add_event(
            session,
            run,
            EventKind.CONFIRMATION_REQUESTED,
            tool_key=spec.key,
            payload={"arguments": body.arguments, "label": spec.label},
        )
        await session.commit()
        return RealtimeCallOut(
            ok=False,
            status=409,
            requires_confirmation=True,
            label=spec.label,
            warning=spec.warning,
            output=json.dumps(
                {
                    "status": "awaiting_confirmation",
                    "say": f"Ask whether to go ahead with: {spec.label}.",
                }
            ),
        )

    if resolved.requires_confirmation:
        await service.add_event(session, run, EventKind.CONFIRMED, tool_key=spec.key)

    await service.add_event(
        session,
        run,
        EventKind.TOOL_CALL,
        tool_key=spec.key,
        payload={"arguments": body.arguments, "label": spec.label, "realtime": True},
    )
    await session.commit()

    outcome = await agent.run_tool(
        spec, body.arguments, session_cookie=_session_cookie(request, settings)
    )
    run.tool_calls = (run.tool_calls or 0) + 1
    await service.add_event(
        session,
        run,
        EventKind.TOOL_RESULT,
        tool_key=spec.key,
        payload={
            "ok": outcome.ok,
            "status": outcome.status,
            "ms": outcome.ms,
            "summary": outcome.text[:500],
        },
    )
    await session.commit()

    return RealtimeCallOut(ok=outcome.ok, status=outcome.status, output=outcome.text)


@router.post(
    "/realtime/session/{run_id}/end",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Close a spoken conversation, and report what it used",
)
async def realtime_end(
    run_id: uuid.UUID,
    user: CurrentUser,
    session: Session,
    usage: RealtimeUsageIn | None = None,
) -> None:
    """Mark the conversation finished, and record what OpenAI charged for it.

    The usage body is how a spoken conversation gets a cost at all. OpenAI runs
    the realtime loop and bills it directly, so the tokens never pass through
    this process; the only place they exist on our side is the ``response.done``
    events the browser receives. The client adds them up and sends the totals
    here, once, as the session closes.

    That makes the figure a report rather than a measurement, and it is stored
    as one — see ``AssistantVoiceUsage.source``. A session whose tab was closed
    sends nothing and costs nothing on the screen, which is the honest failure:
    the alternative, guessing from wall-clock seconds, would put a number there
    that looks exact and is not.

    The body is optional so that a client that only wants to close the run, or
    an older one that does not know about this, still can.
    """
    try:
        run = await service.get_run(session, run_id)
    except AssistantError as exc:
        raise _translate(exc) from exc
    if run.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run")

    changed = False
    if usage is not None and not run.voice_usage_reported:
        settings = await service.get_settings(session)
        await service.record_realtime_usage(
            session,
            user_id=user.id,
            run=run,
            model_key=run.model_key,
            voice=settings.voice,
            usage=usage.model_dump(),
        )
        # Once, per run. A client that retries the close — or one that is left
        # open in two tabs — must not bill the same session twice.
        run.voice_usage_reported = True
        changed = True
    if run.is_open:
        run.status = RunStatus.COMPLETED
        run.finished_at = datetime.now(UTC)
        changed = True
    if changed:
        await session.commit()


# ── administration: settings and models ────────────────────────────────


def _settings_out(row: Any, settings: Settings) -> SettingsOut:
    out = SettingsOut.model_validate(row)
    out.openai_configured = settings.openai_configured
    return out


@admin_router.get("/settings", response_model=SettingsOut, summary="The assistant's settings")
async def read_settings(admin: SuperAdmin, session: Session, settings: Config) -> SettingsOut:
    return _settings_out(await service.get_settings(session), settings)


@admin_router.patch("/settings", response_model=SettingsOut, summary="Change the settings")
async def update_settings(
    body: SettingsIn,
    request: Request,
    admin: SuperAdmin,
    session: Session,
    settings: Config,
) -> SettingsOut:
    """Only the fields present change. Sending a cap as null removes it."""
    try:
        row = await service.update_settings(
            session,
            actor_id=admin.id,
            changes=body.model_dump(exclude_unset=True),
        )
    except AssistantError as exc:
        raise _translate(exc) from exc
    _forget(request)
    return _settings_out(row, settings)


@admin_router.get("/models", response_model=list[ModelOut], summary="Models to choose from")
async def list_models(admin: SuperAdmin, session: Session) -> list[ModelOut]:
    current = await service.get_settings(session)
    out = []
    for model in await service.list_models(session):
        row = ModelOut.model_validate(model)
        row.active = model.key == current.model_key
        out.append(row)
    return out


@admin_router.patch(
    "/models/{key}", response_model=ModelOut, summary="Enable, disable or reprice a model"
)
async def update_model(
    key: str, body: ModelIn, request: Request, admin: SuperAdmin, session: Session
) -> ModelOut:
    """Prices are per million tokens and drive every cost figure the panel shows."""
    try:
        model = await service.update_model(
            session, key, actor_id=admin.id, changes=body.model_dump(exclude_unset=True)
        )
    except AssistantError as exc:
        raise _translate(exc) from exc
    _forget(request)
    current = await service.get_settings(session)
    out = ModelOut.model_validate(model)
    out.active = model.key == current.model_key
    return out


def _voice_model_out(model: Any, settings: Any) -> VoiceModelOut:
    out = VoiceModelOut.model_validate(model)
    out.active = model.key == (
        settings.voice_model if model.kind == "speech" else settings.realtime_model
    )
    return out


@admin_router.get(
    "/voice-models",
    response_model=list[VoiceModelOut],
    summary="What the voice costs, per model",
)
async def list_voice_models(admin: SuperAdmin, session: Session) -> list[VoiceModelOut]:
    """The speech and realtime models, with the prices their cost is worked out from.

    Kept apart from ``/models`` because they are not billed in the same unit:
    speech is charged per character of text, a spoken conversation per token
    with audio dearer than text by an order of magnitude. One list showing
    both under one set of column headings would be a list where most of the
    numbers meant nothing.
    """
    current = await service.get_settings(session)
    return [
        _voice_model_out(model, current) for model in await service.list_voice_models(session)
    ]


@admin_router.patch(
    "/voice-models/{key}",
    response_model=VoiceModelOut,
    summary="Enable, disable or reprice a voice model",
)
async def update_voice_model(
    key: str, body: VoiceModelIn, request: Request, admin: SuperAdmin, session: Session
) -> VoiceModelOut:
    """Change what a voice model is reckoned to cost.

    Editable for the same reason the chat model's prices are: OpenAI moves them,
    and a stale price does not fail — it quietly makes every figure on the cost
    screen wrong. Prices belonging to the other kind are ignored rather than
    refused, so a screen may send the whole form back without pruning it.
    """
    try:
        model = await service.update_voice_model(
            session, key, actor_id=admin.id, changes=body.model_dump(exclude_unset=True)
        )
    except AssistantError as exc:
        raise _translate(exc) from exc
    _forget(request)
    return _voice_model_out(model, await service.get_settings(session))


# ── administration: what it may do ─────────────────────────────────────


@admin_router.get(
    "/policies",
    response_model=list[ModulePolicyOut],
    summary="What the assistant may read and write, per module and tool",
)
async def read_policies(admin: SuperAdmin, session: Session) -> list[ModulePolicyOut]:
    """Every module with its tools, showing both what is set and what applies.

    ``effective_*`` is what a turn would actually see once the module policy and
    the global default are folded in — the column that matters, and the one a
    per-tool override is easy to get wrong without.
    """
    settings = await service.get_settings(session)
    matrix = service.policy_matrix(
        settings, await service.module_policies(session), await service.tool_policies(session)
    )
    return [ModulePolicyOut.model_validate(row) for row in matrix]


@admin_router.patch(
    "/policies/modules/{module_key}",
    response_model=list[ModulePolicyOut],
    summary="Change one module's permissions",
)
async def update_module_policy(
    module_key: str,
    body: ModulePolicyIn,
    request: Request,
    admin: SuperAdmin,
    session: Session,
) -> list[ModulePolicyOut]:
    try:
        await service.update_module_policy(
            session, module_key, actor_id=admin.id, changes=body.model_dump(exclude_unset=True)
        )
    except AssistantError as exc:
        raise _translate(exc) from exc
    _forget(request)
    return await read_policies(admin, session)


@admin_router.patch(
    "/policies/tools/{tool_key}",
    response_model=list[ModulePolicyOut],
    summary="Change one tool's permissions",
)
async def update_tool_policy(
    tool_key: str,
    body: ToolPolicyIn,
    request: Request,
    admin: SuperAdmin,
    session: Session,
) -> list[ModulePolicyOut]:
    try:
        await service.update_tool_policy(
            session, tool_key, actor_id=admin.id, changes=body.model_dump(exclude_unset=True)
        )
    except AssistantError as exc:
        raise _translate(exc) from exc
    _forget(request)
    return await read_policies(admin, session)


# ── administration: who may use it ─────────────────────────────────────


@admin_router.get("/rules", response_model=list[AccessRuleOut], summary="Access rules")
async def list_rules(admin: SuperAdmin, session: Session) -> list[AccessRuleOut]:
    return [AccessRuleOut.model_validate(r) for r in await service.list_rules(session)]


@admin_router.post(
    "/rules",
    response_model=AccessRuleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Release the assistant to somebody, or withhold it",
)
async def create_rule(
    body: AccessRuleIn, request: Request, admin: SuperAdmin, session: Session
) -> AccessRuleOut:
    """Name a user, a team or a role. Block always beats allow.

    In ``allow_list`` mode nobody reaches the assistant without a matching allow
    rule, which is how it is released to one team at a time. In ``everyone``
    mode allow rules do nothing and block rules take people away.
    """
    try:
        rule = await service.create_rule(
            session,
            actor_id=admin.id,
            subject_type=body.subject_type,
            subject=body.subject,
            effect=body.effect,
            enabled=body.enabled,
            note=body.note,
        )
    except AssistantError as exc:
        raise _translate(exc) from exc
    _forget(request)
    return AccessRuleOut.model_validate(rule)


@admin_router.patch(
    "/rules/{rule_id}", response_model=AccessRuleOut, summary="Turn a rule on or off"
)
async def update_rule(
    rule_id: uuid.UUID,
    body: AccessRulePatch,
    request: Request,
    admin: SuperAdmin,
    session: Session,
) -> AccessRuleOut:
    try:
        rule = await service.update_rule(
            session, rule_id, changes=body.model_dump(exclude_unset=True)
        )
    except AssistantError as exc:
        raise _translate(exc) from exc
    _forget(request)
    return AccessRuleOut.model_validate(rule)


@admin_router.delete(
    "/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a rule"
)
async def delete_rule(
    rule_id: uuid.UUID, request: Request, admin: SuperAdmin, session: Session
) -> None:
    try:
        await service.delete_rule(session, rule_id)
    except AssistantError as exc:
        raise _translate(exc) from exc
    _forget(request)


# ── administration: runs, logs and analytics ───────────────────────────


def _run_out(run: AssistantRun) -> RunOut:
    return RunOut(
        id=run.id,
        conversation_id=run.conversation_id,
        user_id=run.user_id,
        user_email=run.user.email,
        user_name=run.user.display_name,
        status=run.status,
        model_key=run.model_key,
        reasoning_effort=run.reasoning_effort,
        started_at=run.started_at,
        finished_at=run.finished_at,
        user_text=run.user_text,
        answer_text=run.answer_text,
        error=run.error,
        input_tokens=run.input_tokens,
        cached_input_tokens=run.cached_input_tokens,
        output_tokens=run.output_tokens,
        reasoning_tokens=run.reasoning_tokens,
        cost_usd=run.cost_usd,
        tool_calls=run.tool_calls,
        rounds=run.rounds,
        cancel_requested=run.cancel_requested,
    )


@admin_router.get("/runs", response_model=RunPage, summary="Every turn anyone has taken")
async def list_runs(
    admin: SuperAdmin,
    session: Session,
    user_id: Annotated[uuid.UUID | None, Query(description="One person's runs")] = None,
    team_id: Annotated[uuid.UUID | None, Query(description="Runs by that team's members")] = None,
    run_status: Annotated[
        str | None, Query(alias="status", description="running, completed, failed, blocked, …")
    ] = None,
    since: Annotated[date | None, Query(description="From this day (UTC), inclusive")] = None,
    until: Annotated[date | None, Query(description="To this day (UTC), inclusive")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunPage:
    runs, total = await service.list_runs(
        session,
        user_id=user_id,
        team_id=team_id,
        status=run_status,
        since=datetime.combine(since, datetime.min.time(), tzinfo=UTC) if since else None,
        until=(
            datetime.combine(until + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
            if until
            else None
        ),
        limit=limit,
        offset=offset,
    )
    return RunPage(runs=[_run_out(r) for r in runs], total=total)


@admin_router.get(
    "/runs/live", response_model=list[RunOut], summary="Turns happening right now"
)
async def live_runs(admin: SuperAdmin, session: Session) -> list[RunOut]:
    """Running, or parked waiting for somebody to confirm an action."""
    return [_run_out(r) for r in await service.open_runs(session)]


@admin_router.get(
    "/runs/{run_id}", response_model=RunDetailOut, summary="One turn, with its full log"
)
async def read_run(run_id: uuid.UUID, admin: SuperAdmin, session: Session) -> RunDetailOut:
    """Every event in order: the message, each tool call and result, each
    confirmation, every refusal, the token usage and any error."""
    try:
        run = await service.get_run(session, run_id, with_events=True)
    except AssistantError as exc:
        raise _translate(exc) from exc
    detail = RunDetailOut(
        **_run_out(run).model_dump(),
        events=[
            {
                "seq": e.seq,
                "kind": e.kind,
                "tool_key": e.tool_key,
                "payload": e.payload,
                "created_at": e.created_at,
            }
            for e in run.events
        ],
        pending=[PendingActionOut(**a) for a in (run.pending or [])] or None,
    )
    return detail


@admin_router.post(
    "/runs/{run_id}/cancel", response_model=RunOut, summary="Stop a turn that is running"
)
async def cancel_run(run_id: uuid.UUID, admin: SuperAdmin, session: Session) -> RunOut:
    """A running turn stops at its next step; a paused one stops immediately."""
    try:
        run = await service.get_run(session, run_id)
        await service.request_cancel(session, run)
    except AssistantError as exc:
        raise _translate(exc) from exc
    return _run_out(run)


@admin_router.get("/analytics", response_model=AnalyticsOut, summary="Usage and cost")
async def analytics(
    admin: SuperAdmin,
    session: Session,
    since: Annotated[
        date | None, Query(description="From this day. Defaults to 30 days ago.")
    ] = None,
    until: Annotated[
        date | None, Query(description="To this day, inclusive. Defaults to today.")
    ] = None,
) -> AnalyticsOut:
    """Totals and breakdowns by day, person, team, model and tool.

    A person on two teams counts for both, so the team rows add up to more than
    the total. That is what a team lead wants to see and worth knowing when
    reading the numbers.
    """
    today = datetime.now(UTC).date()
    end = until or today
    start = since or (end - timedelta(days=29))
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="'since' must not be after 'until'"
        )
    return AnalyticsOut.model_validate(await service.analytics(session, since=start, until=end))


@admin_router.get(
    "/catalogue", summary="Every tool the assistant knows, whatever the policy says"
)
async def catalogue(admin: SuperAdmin) -> dict[str, Any]:
    """The code catalogue itself, before any policy is applied.

    Includes the tools that are only planned, which appear nowhere else: they
    are offered to nobody and have no policy row, so this is the only way to
    see what is coming alongside what is here.
    """
    return {
        "modules": [
            {
                "key": group.key,
                "name": group.name,
                "gate": group.gate,
                "description": group.description,
                #: The write restriction the module ships with, before any
                #: super admin edit. What ``/policies`` would go back to.
                "default_write_roles": (
                    list(group.write_roles) if group.write_roles else None
                ),
                "tools": [
                    {
                        "key": spec.key,
                        "label": spec.label,
                        "kind": spec.kind,
                        "method": spec.method,
                        "path": spec.path,
                        "description": spec.description,
                        "warning": spec.warning,
                        "status": spec.status,
                        "deferred": spec.deferred,
                    }
                    for spec in TOOLS_BY_KEY.values()
                    if spec.module_key == group.key
                ],
            }
            for group in GROUPS_BY_KEY.values()
        ]
    }
