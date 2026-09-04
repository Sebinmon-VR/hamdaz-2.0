"""Read the organisation's directory.

Nothing here writes. This module answers "who works here" straight from Entra;
deciding which of those people become ERP records is the team module's job.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.auth.deps import CurrentUser
from app.directory.graph import GraphDirectory, GraphError, OrgUser
from app.directory.schemas import OrgUserOut, OrgUserPage

router = APIRouter(prefix="/directory", tags=["directory"])


def get_directory(request: Request) -> GraphDirectory:
    return request.app.state.graph


def _matches(user: OrgUser, needle: str) -> bool:
    haystack = " ".join(
        part
        for part in (
            user.display_name,
            user.email,
            user.user_principal_name,
            user.job_title,
            user.department,
        )
        if part
    )
    return needle in haystack.casefold()


@router.get("/users", response_model=OrgUserPage, summary="Everyone in the organisation")
async def list_org_users(
    _: CurrentUser,
    directory: Annotated[GraphDirectory, Depends(get_directory)],
    search: Annotated[
        str | None, Query(description="Match name, email, title or department")
    ] = None,
    include_guests: Annotated[bool, Query(description="Include external guest accounts")] = False,
    include_disabled: Annotated[bool, Query(description="Include disabled accounts")] = False,
    limit: Annotated[int, Query(ge=1, le=999)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrgUserPage:
    try:
        users = await directory.list_users(
            include_guests=include_guests, include_disabled=include_disabled
        )
    except GraphError as exc:
        # 502, not 500: the failure is upstream, and saying so is the difference
        # between "our bug" and "Graph is unhappy" when this shows up in a log.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the organisation directory",
        ) from exc

    if search:
        needle = search.strip().casefold()
        users = [u for u in users if _matches(u, needle)]

    window = users[offset : offset + limit]
    return OrgUserPage(
        total=len(users),
        count=len(window),
        offset=offset,
        limit=limit,
        users=[OrgUserOut.from_domain(u) for u in window],
    )


@router.get("/users/{object_id}", response_model=OrgUserOut, summary="One person by object id")
async def get_org_user(
    object_id: str,
    _: CurrentUser,
    directory: Annotated[GraphDirectory, Depends(get_directory)],
) -> OrgUserOut:
    try:
        return OrgUserOut.from_domain(await directory.get_user(object_id))
    except GraphError as exc:
        if "not found" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such user in the directory"
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the organisation directory",
        ) from exc
