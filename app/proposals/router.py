"""Proposal tasks, scoped to the person asking.

Two independent gates, and both must pass:

1. **Module access** — the caller must belong to a team that has been granted the
   ``proposals`` module. That is the visibility model doing its job.
2. **Ownership** — the rows returned are the ones assigned to *them*. This is not
   a filter the caller can widen: their SharePoint lookup id is derived from the
   session, never taken from the request.

There is deliberately no "all tasks" endpoint. Per the brief, each person sees
their own; a team-wide view would be a separate, explicitly authorised addition.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import service as access_service
from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.proposals.analytics import WorkloadCache, scope_for_team
from app.proposals.schemas import ColumnOut, MyTasksOut, TaskOut, WorkloadOut
from app.proposals.sharepoint import SharePointError, SharePointProposals
from app.roles.deps import AdminUser, CurrentRoles
from app.teams import service as teams_service
from app.teams.service import TeamError

router = APIRouter(prefix="/proposals", tags=["proposals"])

Session = Annotated[AsyncSession, Depends(get_session)]

MODULE_KEY = "proposals"


def get_sharepoint(request: Request) -> SharePointProposals:
    return request.app.state.sharepoint


def get_workload_cache(request: Request) -> WorkloadCache:
    return request.app.state.workload_cache


async def require_module(
    user: CurrentUser, roles: CurrentRoles, session: Session
) -> None:
    """The caller's team must have been granted the proposals module."""
    if not await access_service.can_reach(
        session, user_id=user.id, global_roles=roles, module_key=MODULE_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your team does not have the Proposals module. "
                "A super admin can grant it."
            ),
        )


@router.get(
    "/my-tasks",
    response_model=MyTasksOut,
    summary="The proposal tasks assigned to the caller",
)
async def my_tasks(
    user: CurrentUser,
    session: Session,
    sharepoint: Annotated[SharePointProposals, Depends(get_sharepoint)],
    _: Annotated[None, Depends(require_module)],
    open_only: Annotated[
        bool, Query(description="Hide tasks whose status is Completed")
    ] = True,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> MyTasksOut:
    try:
        # Derived from the session. There is no request parameter that can
        # change whose tasks these are.
        lookup_id = await sharepoint.lookup_id_for(user.email)
        if lookup_id is None:
            # Not an error: they simply have no presence on that SharePoint site,
            # so there is nothing that could be assigned to them.
            return MyTasksOut(
                email=user.email,
                sharepoint_user_id=None,
                in_sharepoint=False,
                total=0,
                open_count=0,
                tasks=[],
            )

        tasks = await sharepoint.tasks_assigned_to(lookup_id, limit=limit)
    except SharePointError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read the Proposals list",
        ) from exc

    open_tasks = [t for t in tasks if t.is_open]
    shown = open_tasks if open_only else tasks

    # Soonest deadline first, by BCD rather than DueDate — see ProposalTask.deadline.
    shown = sorted(shown, key=lambda t: (t.deadline is None, t.deadline or ""))

    return MyTasksOut(
        email=user.email,
        sharepoint_user_id=lookup_id,
        in_sharepoint=True,
        total=len(tasks),
        open_count=len(open_tasks),
        tasks=[TaskOut.from_domain(t) for t in shown],
    )


@router.get(
    "/columns",
    response_model=list[ColumnOut],
    summary="The Proposals list schema",
)
async def columns(
    _user: CurrentUser,
    _: Annotated[None, Depends(require_module)],
    sharepoint: Annotated[SharePointProposals, Depends(get_sharepoint)],
) -> list[ColumnOut]:
    # Lets a client render choice fields without hard-coding SharePoint's options.
    try:
        return [ColumnOut(**c) for c in await sharepoint.list_columns()]
    except SharePointError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read the Proposals list",
        ) from exc


@router.get(
    "/workload",
    response_model=WorkloadOut,
    summary="Per-person proposal counts, for admins",
)
async def workload(
    _: AdminUser,
    session: Session,
    sharepoint: Annotated[SharePointProposals, Depends(get_sharepoint)],
    cache: Annotated[WorkloadCache, Depends(get_workload_cache)],
    team: Annotated[
        str | None,
        Query(description="Restrict to one team's members. Omit for everyone."),
    ] = None,
    refresh: Annotated[
        bool, Query(description="Re-sweep SharePoint instead of using the cache")
    ] = False,
) -> WorkloadOut:
    """Every person's totals in one call.

    Admin-only, and deliberately not gated on the proposals *module*: this is an
    organisation-wide management view, not the team-scoped worker view that
    ``/my-tasks`` serves.
    """
    scope = None
    only: set[str] | None = None

    try:
        if team is not None:
            resolved = await teams_service.get_team(session, team)
            scope = await scope_for_team(session, resolved, sharepoint)
            # Not part of the response; it is the filter itself.
            only = scope.pop("lookup_ids")

        summary = await cache.get(sharepoint, refresh=refresh, only=only)
    except TeamError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except SharePointError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read the Proposals list",
        ) from exc

    return WorkloadOut(scope=scope, **summary)
