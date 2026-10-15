"""The mirror's HTTP surface: is the copy current, refresh it, who is next, and
Graph's webhook for changes to the Proposals list.

The paths stay under ``/intake`` because Graph's list subscription was made
with ``/intake/notifications`` as its address; moving it would silently stop
the notifications. The mail half — the watched mailbox, its settings and its
log — was removed on 2026-10-14.

Super admin only, except the webhook, which cannot be authenticated by a
session because the caller is Microsoft — it is authenticated by echoing a
validation token and by checking the secret we gave Graph when we subscribed.
"""

from __future__ import annotations

import contextlib
import secrets
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import live as live_scores
from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.intake.schemas import MirrorStatusOut, StandingOut
from app.models.proposal_index import MirrorState, ProposalIndexItem
from app.models.team import Team
from app.models.user import User
from app.proposals import mirror as mirror_service
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles
from app.teams import service as teams_service
from app.teams.service import TeamError

router = APIRouter(prefix="/intake", tags=["proposals mirror"])
#: Not authenticated by session: the caller is Microsoft, not a person. Kept
#: separate so nothing here inherits a dependency that would refuse them.
webhook_router = APIRouter(prefix="/intake", tags=["proposals mirror"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can see or change the Proposals mirror.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


def _worker(request: Request):
    worker = getattr(request.app.state, "intake_worker", None)
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The mirror worker is not running in this process.",
        )
    return worker


# ── the mirror ─────────────────────────────────────────────────────────


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
    return MirrorStatusOut(
        rows=rows,
        last_sync_at=state.last_sync_at if state else None,
        rows_read=state.rows_read if state else 0,
        rows_changed=state.rows_changed if state else 0,
        duration_ms=state.duration_ms if state else 0,
        last_error=state.last_error if state else None,
        subscription_id=state.subscription_id if state else None,
        subscription_expires_at=state.subscription_expires_at if state else None,
        publish_enabled=config.analytics_publish_enabled,
        publish_list_url=config.analytics_list_url,
    )


@router.post("/mirror/sync", response_model=MirrorStatusOut, summary="Refresh it now")
async def sync_mirror(request: Request, admin: SuperAdmin, session: Session) -> MirrorStatusOut:
    """Pull the list and rewrite the ranking, without waiting for the timer.

    Reads SharePoint; writes nothing to it.
    """
    # Forced: somebody asked explicitly, so a background flag being off
    # is not a reason to refuse them.
    await _worker(request).sync_once(force=True)
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
    """The current ranking. Rank 1 is next. A stored table, not a computation."""
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
    summary="Where Graph posts when the Proposals list changes",
)
async def graph_notification(
    request: Request,
    session: Session,
    validationToken: Annotated[str | None, Query()] = None,
) -> Response:
    """Graph's change notifications for the Proposals list. Not a route a person calls.

    On subscribing Graph posts a ``validationToken`` and expects it echoed back
    as plain text within seconds — so that branch must come first and must not
    touch the database. Afterwards it posts batches of notifications, each
    carrying the ``clientState`` we set at subscribe time, which is checked
    before anything is acted on: an endpoint that trusts whatever posts to it is
    an open door. A notification says only "the list changed", never which row,
    so the answer is to wake the mirror loop, which re-reads the whole list.

    Always 202. Graph retries anything else, and a retry storm caused by our
    own bug helps nobody.
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

    mirror = await mirror_service.state(session)
    list_secret = mirror.subscription_secret
    for item in payload.get("value", []) or []:
        if list_secret and item.get("clientState") == list_secret:
            worker.kick()
    return Response(status_code=status.HTTP_202_ACCEPTED)
