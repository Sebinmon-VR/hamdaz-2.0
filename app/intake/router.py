"""The intake's HTTP surface: settings, the log, and Graph's webhook.

Almost all of it is super admin only, and for the same reason the delivery log
is: this is a record of who was told what about whose work, plus the addresses
being watched. The one exception is the webhook, which cannot be authenticated
by a session because the caller is Microsoft — it is authenticated by echoing a
validation token and by checking the secret we gave Graph when we subscribed.
"""

from __future__ import annotations

import contextlib
import secrets
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import live as live_scores
from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.intake import service
from app.intake.schemas import (
    IntakeMessageOut,
    IntakePage,
    IntakeSettingsIn,
    IntakeSettingsOut,
    MirrorStatusOut,
    StandingOut,
)
from app.intake.service import IntakeError
from app.models.intake import IntakeMessage, IntakeStatus
from app.models.proposal_index import MirrorState, ProposalIndexItem
from app.models.team import Team
from app.models.user import User
from app.proposals import mirror as mirror_service
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles
from app.teams import service as teams_service
from app.teams.service import TeamError

router = APIRouter(prefix="/intake", tags=["mail intake"])
#: Not under ``/intake`` and not authenticated by session: the caller is
#: Microsoft, not a person. Kept separate so nothing here inherits a dependency
#: that would refuse them.
webhook_router = APIRouter(prefix="/intake", tags=["mail intake"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    """Only a super admin configures the intake or reads its log.

    The log holds who was told what about whose work, and the settings decide
    whose mailbox is read — neither is anybody else's business, including a
    CEO's.
    """
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can see or change the mail intake.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


def _worker(request: Request):
    worker = getattr(request.app.state, "intake_worker", None)
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The intake worker is not running in this process.",
        )
    return worker


def _translate(exc: IntakeError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ── settings ───────────────────────────────────────────────────────────


@router.get("/settings", response_model=IntakeSettingsOut, summary="What is watched")
async def read_settings(admin: SuperAdmin, session: Session) -> IntakeSettingsOut:
    return IntakeSettingsOut.model_validate(await service.get_settings(session))


@router.patch("/settings", response_model=IntakeSettingsOut, summary="Change it")
async def update_settings(
    body: IntakeSettingsIn, admin: SuperAdmin, session: Session
) -> IntakeSettingsOut:
    """Only the fields sent change.

    Note ``create_in_sharepoint``. It ships off, and turning it on is the
    moment this system starts writing rows into the live Proposals list the
    team works in. Everything else can be undone; that cannot.
    """
    try:
        row = await service.update_settings(
            session, actor_id=admin.id, changes=body.model_dump(exclude_unset=True)
        )
    except IntakeError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return IntakeSettingsOut.model_validate(row)


# ── the log ────────────────────────────────────────────────────────────


def _message_out(row: IntakeMessage) -> IntakeMessageOut:
    return IntakeMessageOut(
        id=row.id,
        received_at=row.received_at,
        sender_email=row.sender_email,
        sender_name=row.sender_name,
        subject=row.subject,
        status=row.status,
        category=row.category,
        is_reopened=row.is_reopened,
        confidence=float(row.confidence) if row.confidence is not None else None,
        reasoning=row.reasoning,
        extracted=row.extracted or {},
        matched_item_id=row.matched_item_id,
        match_confidence=(
            float(row.match_confidence) if row.match_confidence is not None else None
        ),
        match_reason=row.match_reason,
        candidates=row.candidates or [],
        action=row.action,
        assigned_user_id=row.assigned_user_id,
        assigned_name=row.assigned_user.display_name if row.assigned_user else None,
        assigned_reason=row.assigned_reason,
        created_item_id=row.created_item_id,
        would_create=row.would_create,
        would_update=row.would_update,
        notified_teams=row.notified_teams,
        notified_user_ids=list(row.notified_user_ids or []),
        error=row.error,
        processed_at=row.processed_at,
        cost_usd=float(row.cost_usd) if row.cost_usd is not None else None,
        web_link=row.web_link,
    )


@router.get("/messages", response_model=IntakePage, summary="Every mail it has seen")
async def list_messages(
    admin: SuperAdmin,
    session: Session,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    category: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IntakePage:
    """Including the ones it ignored, which are the useful ones.

    "Why did nothing happen when I sent that" is the question this exists to
    answer, and only the ignored rows answer it.
    """
    rows, total = await service.listing(
        session, status=status_filter, category=category, limit=limit, offset=offset
    )
    counts = {
        row.status: int(row.n)
        for row in (
            await session.execute(
                select(IntakeMessage.status, func.count().label("n")).group_by(
                    IntakeMessage.status
                )
            )
        ).all()
    }
    return IntakePage(
        messages=[_message_out(r) for r in rows],
        total=total,
        counts={s.value: counts.get(s.value, 0) for s in IntakeStatus},
    )


@router.get(
    "/messages/{message_id}", response_model=IntakeMessageOut, summary="One in full"
)
async def read_message(
    message_id: uuid.UUID, admin: SuperAdmin, session: Session
) -> IntakeMessageOut:
    row = await session.get(IntakeMessage, message_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such message")
    return _message_out(row)


@router.post(
    "/messages/{message_id}/retry",
    response_model=IntakeMessageOut,
    summary="Put one back through",
)
async def retry_message(
    message_id: uuid.UUID,
    request: Request,
    admin: SuperAdmin,
    session: Session,
    config: Config,
) -> IntakeMessageOut:
    """Run the pipeline over a message again, from where it is now.

    For after a setting changed — a sender added, a threshold lowered, or
    writing switched on. The classification is redone rather than reused,
    because the thing being retried is usually the decision.
    """
    row = await session.get(IntakeMessage, message_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such message")
    worker = _worker(request)
    intake = await service.get_settings(session)
    row.status = IntakeStatus.RECEIVED
    row.error = None
    await session.flush()
    await service.process(
        session, row,
        settings=config,
        intake=intake,
        classifier=worker.classifier,
        matcher=worker.matcher,
        sharepoint=request.app.state.sharepoint,
        http=request.app.state.http,
    )
    await session.commit()
    return _message_out(row)


# ── the mirror and the ranking ─────────────────────────────────────────


@router.get("/mirror", response_model=MirrorStatusOut, summary="Is the local copy current")
async def mirror_status(
    admin: SuperAdmin, session: Session, config: Config
) -> MirrorStatusOut:
    state = await session.get(MirrorState, 1)
    rows = int(
        await session.scalar(
            select(func.count())
            .select_from(ProposalIndexItem)
            .where(ProposalIndexItem.deleted.is_(False))
        )
        or 0
    )
    embedded = int(
        await session.scalar(
            select(func.count())
            .select_from(ProposalIndexItem)
            .where(
                ProposalIndexItem.deleted.is_(False),
                ProposalIndexItem.embedding.is_not(None),
            )
        )
        or 0
    )
    return MirrorStatusOut(
        rows=rows,
        embedded=embedded,
        last_sync_at=state.last_sync_at if state else None,
        rows_read=state.rows_read if state else 0,
        rows_changed=state.rows_changed if state else 0,
        rows_embedded=state.rows_embedded if state else 0,
        duration_ms=state.duration_ms if state else 0,
        last_error=state.last_error if state else None,
        subscription_id=state.subscription_id if state else None,
        subscription_expires_at=state.subscription_expires_at if state else None,
        publish_enabled=config.analytics_publish_enabled,
        publish_list_url=config.analytics_list_url,
    )


@router.post("/mirror/sync", response_model=MirrorStatusOut, summary="Refresh it now")
async def sync_mirror(
    request: Request,
    admin: SuperAdmin,
    session: Session,
    embed: Annotated[bool, Query(description="Also embed changed rows.")] = True,
) -> MirrorStatusOut:
    """Pull the list and rewrite the ranking, without waiting for the timer.

    Reads SharePoint; writes nothing to it.
    """
    # Forced: somebody asked explicitly, so a background flag being off
    # is not a reason to refuse them.
    await _worker(request).sync_once(embed=embed, force=True)
    return await mirror_status(admin, session, get_settings())


@router.post(
    "/mirror/subscription",
    response_model=MirrorStatusOut,
    summary="Ask Graph to tell us when the Proposals list changes",
)
async def subscribe_to_list(
    request: Request, admin: SuperAdmin, session: Session, config: Config
) -> MirrorStatusOut:
    """Make a row edited in SharePoint reach the ranking in seconds.

    Without this the mirror refreshes on its timer, so a task assigned by hand
    in SharePoint is scored up to ``MIRROR_SYNC_SECONDS`` later. With it, Graph
    posts to the webhook below as soon as the list changes and the loop wakes
    at once. The timer keeps running either way — it is the safety net.

    Graph validates the address first, so this refuses outright when the app
    is not reachable over https from the internet. That is the honest outcome
    and it is why this is a button and not something that runs at start-up.
    """
    base = (config.public_base_url or str(request.base_url)).rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Graph will only post to an https address it can reach. Set "
                "PUBLIC_BASE_URL to this app's public URL."
            ),
        )
    secret = secrets.token_urlsafe(24)
    sharepoint = request.app.state.sharepoint
    try:
        created = await sharepoint.subscribe_list(
            config.sharepoint_site_id,
            config.sharepoint_proposals_list_id,
            notification_url=f"{base}{config.api_prefix}/intake/notifications",
            secret=secret,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    state = await mirror_service.state(session)
    state.subscription_id = created.get("id")
    state.subscription_secret = secret
    expires = created.get("expirationDateTime")
    state.subscription_expires_at = None
    if expires:
        with contextlib.suppress(ValueError):
            state.subscription_expires_at = datetime.fromisoformat(
                str(expires).replace("Z", "+00:00")
            )
    await session.commit()
    return await mirror_status(admin, session, config)


@router.get(
    "/standing", response_model=list[StandingOut], summary="Who gets the next task"
)
async def standing(
    admin: SuperAdmin,
    session: Session,
    team: Annotated[str | None, Query(description="Team handle or id.")] = None,
) -> list[StandingOut]:
    """The current ranking. Rank 1 is next.

    A stored table rather than a computation, which is what lets the intake
    decide who an incoming tender goes to while the email is still being read.
    """
    found: Team | None = None
    if team:
        try:
            found = await teams_service.get_team(session, team)
        except TeamError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such team"
            ) from None
    rows = await live_scores.standing(session, team=found)
    return [
        StandingOut(
            user_id=r.user_id,
            display_name=r.display_name,
            email=r.email,
            rank=r.rank,
            eligible=r.eligible,
            excluded_reason=r.excluded_reason,
            open_tasks=r.open_tasks,
            active_tasks=r.active_tasks,
            overdue_tasks=r.overdue_tasks,
            total_tasks=r.total_tasks,
            days_since_assigned=r.days_since_assigned,
            factors=r.factors or {},
            computed_at=r.computed_at,
            reason=r.reason,
        )
        for r in rows
    ]


# ── Graph's webhook ────────────────────────────────────────────────────


@webhook_router.post(
    "/notifications",
    include_in_schema=False,
    summary="Where Graph posts when mail arrives",
)
async def graph_notification(
    request: Request,
    session: Session,
    validationToken: Annotated[str | None, Query()] = None,
) -> Response:
    """Graph's change notifications. Not a route a person calls.

    Two things happen here and both are required by Graph. On subscribing it
    posts a ``validationToken`` and expects it echoed back as plain text within
    seconds — so that branch must come first and must not touch the database.
    Afterwards it posts batches of notifications, each carrying the
    ``clientState`` we set at subscribe time, which is checked before anything
    is acted on: an endpoint that trusts whatever posts to it is an open door.

    Always 202. Graph retries anything else, and a retry storm caused by our
    own bug helps nobody — what went wrong belongs on the intake row.
    """
    if validationToken:
        return Response(content=validationToken, media_type="text/plain")

    payload: dict[str, Any] = {}
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed is simply ignored
        return Response(status_code=status.HTTP_202_ACCEPTED)

    worker = getattr(request.app.state, "intake_worker", None)
    if worker is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    intake = await service.get_settings(session)
    mirror = await mirror_service.state(session)
    expected = intake.subscription_secret
    list_secret = mirror.subscription_secret

    for item in payload.get("value", []) or []:
        state_token = item.get("clientState")
        # Two subscriptions post here and each carries its own secret. A list
        # notification says only "the list changed", never which row, so the
        # answer is to wake the mirror loop, which re-reads the whole list.
        if list_secret and state_token == list_secret:
            worker.kick()
            continue
        if not intake.enabled or (expected and state_token != expected):
            continue
        resource = str(item.get("resourceData", {}).get("id") or "")
        if resource:
            # Handled inline rather than in a background task: Graph allows
            # thirty seconds, one message is well inside that, and a task
            # spawned here would outlive the request with nothing watching it.
            await worker.process_message_id(resource)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/subscription", summary="Ask Graph to notify us")
async def create_subscription(
    request: Request, admin: SuperAdmin, session: Session, config: Config
) -> dict[str, Any]:
    """Subscribe to the watched mailbox, so mail is handled in seconds.

    Graph validates the endpoint before creating anything: it posts to the URL
    and expects the token echoed. So this fails outright if the app is not
    reachable from the internet, which is the honest outcome — a subscription
    that silently never fires would be worse.

    Polling continues either way. It is the safety net, and Microsoft's own
    advice is not to rely on notifications alone.
    """
    intake = await service.get_settings(session)
    if not intake.mailbox:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set the mailbox first.",
        )
    base = (config.public_base_url or str(request.base_url)).rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Graph will only post to an https address it can reach. Set "
                "PUBLIC_BASE_URL to this app's public URL."
            ),
        )
    secret = secrets.token_urlsafe(24)
    try:
        created = await request.app.state.mail_reader.subscribe(
            intake.mailbox,
            notification_url=f"{base}{config.api_prefix}/intake/notifications",
            secret=secret,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    intake.subscription_id = created.get("id")
    intake.subscription_secret = secret
    expires = created.get("expirationDateTime")
    if expires:
        try:
            intake.subscription_expires_at = datetime.fromisoformat(
                str(expires).replace("Z", "+00:00")
            )
        except ValueError:
            intake.subscription_expires_at = None
    await session.commit()
    return {
        "subscription_id": intake.subscription_id,
        "expires_at": intake.subscription_expires_at,
    }
