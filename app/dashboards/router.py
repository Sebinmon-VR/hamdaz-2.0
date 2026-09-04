"""Each team's own dashboard.

``GET /teams/{ref}/dashboard`` renders that team's cards with that team's data.
Which cards exist is decided by the modules the team holds, so a team never sees
a card for something it cannot reach.

Arranging a dashboard is an organisation admin's job (super admin, CEO or
manager) — one step looser than module visibility, which stays super-admin-only.
Deciding what a team *can* see and arranging what they *do* see are different
sized decisions.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.dashboards.widgets  # noqa: F401 — importing registers the widgets
from app.auth.deps import CurrentUser
from app.core.db import get_session, get_session_factory
from app.dashboards import registry, service
from app.dashboards.schemas import (
    DashboardOut,
    LayoutOut,
    PlacementOut,
    SetLayout,
    WidgetInfo,
)
from app.dashboards.service import DashboardError, DashboardNotFoundError
from app.directory.router import get_directory
from app.proposals.analytics import WorkloadCache
from app.proposals.router import get_sharepoint, get_workload_cache
from app.roles.deps import AdminUser
from app.teams import service as teams_service
from app.teams.service import TeamError, TeamNotFoundError

router = APIRouter(tags=["dashboards"])

Session = Annotated[AsyncSession, Depends(get_session)]
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Directory = Annotated[object, Depends(get_directory)]
SharePoint = Annotated[object, Depends(get_sharepoint)]
Workload = Annotated[WorkloadCache, Depends(get_workload_cache)]


def _translate(exc: DashboardError | TeamError) -> HTTPException:
    if isinstance(exc, DashboardNotFoundError | TeamNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _widget_info(widget) -> WidgetInfo:
    return WidgetInfo(
        key=widget.key,
        title=widget.title,
        description=widget.description,
        module=widget.module,
        size=widget.size,
        default=widget.default,
        remote=widget.remote,
    )


async def _team(session: AsyncSession, ref: str):
    try:
        return await teams_service.get_team(session, ref)
    except TeamError as exc:
        raise _translate(exc) from exc


# ── the widget catalogue ───────────────────────────────────────────────


@router.get("/widgets", response_model=list[WidgetInfo], summary="Every dashboard widget")
async def list_widgets(_: CurrentUser) -> list[WidgetInfo]:
    return [_widget_info(w) for w in registry.all_widgets()]


# ── a team's dashboard ─────────────────────────────────────────────────


@router.get(
    "/teams/{ref}/dashboard",
    response_model=DashboardOut,
    summary="Render this team's dashboard",
)
async def get_dashboard(
    ref: str,
    viewer: CurrentUser,
    session: Session,
    factory: Factory,
    directory: Directory,
    sharepoint: SharePoint,
    workload_cache: Workload,
    local_only: Annotated[
        bool, Query(description="Skip widgets that call out to Entra or SharePoint")
    ] = False,
) -> DashboardOut:
    team = await _team(session, ref)
    rendered = await service.render(
        team=team,
        viewer=viewer,
        session=session,
        factory=factory,
        directory=directory,
        sharepoint=sharepoint,
        workload_cache=workload_cache,
        include_remote=not local_only,
    )
    return DashboardOut(**rendered)


@router.get(
    "/teams/{ref}/dashboard/layout",
    response_model=LayoutOut,
    summary="The arrangement, without loading any data",
)
async def get_layout(ref: str, _: CurrentUser, session: Session) -> LayoutOut:
    team = await _team(session, ref)
    placements = await service.resolve_layout(session, team)
    available = await service.available_widgets(session, team)

    return LayoutOut(
        team_id=team.id,
        slug=team.slug,
        configured=any(p.configured for p in placements),
        widgets=[
            PlacementOut(
                widget_key=p.widget.key,
                title=p.widget.title,
                module=p.widget.module,
                size=p.widget.size,
                position=p.position,
                enabled=p.enabled,
                options=p.options,
            )
            for p in placements
        ],
        available=[_widget_info(w) for w in available],
    )


@router.put(
    "/teams/{ref}/dashboard/layout",
    response_model=LayoutOut,
    summary="Arrange this team's dashboard",
)
async def set_layout(
    ref: str, payload: SetLayout, actor: AdminUser, session: Session
) -> LayoutOut:
    team = await _team(session, ref)
    try:
        await service.set_layout(
            session,
            team=team,
            entries=[e.model_dump() for e in payload.widgets],
            configured_by_id=actor.id,
        )
    except DashboardError as exc:
        raise _translate(exc) from exc
    return await get_layout(ref, actor, session)


@router.delete(
    "/teams/{ref}/dashboard/layout",
    response_model=LayoutOut,
    summary="Drop the arrangement and fall back to defaults",
)
async def reset_layout(ref: str, actor: AdminUser, session: Session) -> LayoutOut:
    team = await _team(session, ref)
    await service.reset_layout(session, team=team)
    return await get_layout(ref, actor, session)


# ── the caller's own view ──────────────────────────────────────────────


@router.get(
    "/dashboards/me",
    response_model=list[DashboardOut],
    summary="A dashboard for each team the caller belongs to",
)
async def my_dashboards(
    viewer: CurrentUser,
    session: Session,
    factory: Factory,
    directory: Directory,
    sharepoint: SharePoint,
    workload_cache: Workload,
    local_only: Annotated[bool, Query()] = True,
) -> list[DashboardOut]:
    # Defaults to local_only: this can render several teams at once, and one
    # Entra round trip per team is not worth it for a landing page.
    pairs = await teams_service.teams_for_user(session, viewer.id)
    out = []
    for team, _roles in pairs:
        rendered = await service.render(
            team=team,
            viewer=viewer,
            session=session,
            factory=factory,
            directory=directory,
            sharepoint=sharepoint,
            workload_cache=workload_cache,
            include_remote=not local_only,
        )
        out.append(DashboardOut(**rendered))
    return out
