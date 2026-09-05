"""Proposal tasks, scoped to the person asking.

Two independent gates, and both must pass:

1. **Module access** — the caller must belong to a team that has been granted the
   ``proposals`` module. That is the visibility model doing its job.
2. **Ownership** — the rows returned are the ones assigned to *them*. This is not
   a filter the caller can widen: their SharePoint lookup id is derived from the
   session, never taken from the request.

There is deliberately no "all tasks" endpoint. Per the brief, each person sees
their own; a team-wide view would be a separate, explicitly authorised addition.

``/team-tasks`` is that addition, and it is deliberately built the same way
rather than by relaxing anything above. The people whose rows come back are
derived from the team's membership, never named in the request, so a caller
still cannot widen the answer — they can only ask about a team they already
have authority over. Who that is lives in ``app.proposals.oversight``.
"""

from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import service as access_service
from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.hr.documents import UploadError, accept, download_headers
from app.proposals import oversight as oversight_service
from app.proposals.analytics import WorkloadCache, scope_for_team
from app.proposals.schemas import (
    ColumnOut,
    MyTasksOut,
    TaskAttachmentOut,
    TaskOut,
    TaskUpdateIn,
    TeamTasksOut,
    WorkloadOut,
)
from app.proposals.sharepoint import (
    ProposalTask,
    SharePointConsentError,
    SharePointError,
    SharePointProposals,
)
from app.roles.catalogue import ADMIN_ROLES
from app.roles.deps import AdminUser, CurrentRoles, has_any
from app.teams import service as teams_service
from app.teams.service import TeamError

logger = logging.getLogger("hamdaz.proposals")

router = APIRouter(prefix="/proposals", tags=["proposals"])

Session = Annotated[AsyncSession, Depends(get_session)]

MODULE_KEY = "proposals"


def get_sharepoint(request: Request) -> SharePointProposals:
    return request.app.state.sharepoint


def get_workload_cache(request: Request) -> WorkloadCache:
    return request.app.state.workload_cache


def get_team_tasks_cache(request: Request) -> oversight_service.TeamTasksCache:
    return request.app.state.team_tasks_cache


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


@router.get(
    "/team-tasks",
    response_model=TeamTasksOut,
    summary="Every member of one team's proposal tasks, for that team's leadership",
)
async def team_tasks(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    sharepoint: Annotated[SharePointProposals, Depends(get_sharepoint)],
    cache: Annotated[
        oversight_service.TeamTasksCache, Depends(get_team_tasks_cache)
    ],
    team: Annotated[str, Query(description="The team's slug (or id)")],
    open_only: Annotated[
        bool, Query(description="Hide tasks whose status is Completed")
    ] = False,
    limit: Annotated[
        int, Query(ge=1, le=500, description="Rows per member, not for the team")
    ] = 200,
    refresh: Annotated[
        bool, Query(description="Re-read SharePoint instead of using the cache")
    ] = False,
) -> TeamTasksOut:
    """One team's proposal work, member by member, with the rows attached.

    The gate is authority over *this* team: an administrator, or its own lead or
    manager. It is checked against the team resolved from the request, so a lead
    asking about somebody else's team is refused rather than served.

    ``open_only`` defaults to False here, unlike ``/my-tasks``. A lead needs the
    closed rows to see what a person actually got through, and the client filters
    them; asking twice for one screen would be the only alternative.
    """
    try:
        resolved = await teams_service.get_team(session, team)
    except TeamError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    allowed = await oversight_service.oversight(
        session, user=user, roles=roles, team_id=resolved.id
    )
    if not allowed.may_see:
        # 403 rather than 404: the caller is authenticated and the team plainly
        # exists — they can see it on the teams list. Pretending otherwise would
        # make the API harder to use without making it safer.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=allowed.reason)

    try:
        result = await oversight_service.team_tasks(
            session,
            team=resolved,
            sharepoint=sharepoint,
            cache=cache,
            open_only=open_only,
            limit=limit,
            refresh=refresh,
        )
    except SharePointError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read the Proposals list",
        ) from exc

    # The service works in domain rows so it stays testable without pydantic;
    # the wire shape is put on here, at the edge, as the other endpoints do.
    result["members"] = [
        {**member, "tasks": [TaskOut.from_domain(t) for t in member["tasks"]]}
        for member in result["members"]
    ]
    return TeamTasksOut(**result)


# ── one task: its files, and editing it ────────────────────────────────
#
# The ownership rule is the module's own, applied to writes: the person a task
# is assigned to may work on it, admins may work on any, and nobody else gets
# an answer at all. 404 rather than 403 for the excluded, because "there is a
# task 412 and you may not see it" is itself information about the pipeline.


async def _authorised_task(
    task_id: str,
    user,
    roles: set[str],
    sp: SharePointProposals,
) -> ProposalTask:
    try:
        task = await sp.task(task_id)
    except SharePointError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such task"
        ) from exc

    if has_any(roles, ADMIN_ROLES):
        return task
    lookup = await sp.lookup_id_for(user.email)
    if lookup is not None and task.assigned_to_lookup_id == lookup:
        return task
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such task")


def _translate_sp(exc: Exception) -> HTTPException:
    if isinstance(exc, SharePointConsentError):
        # Configuration, not failure: the message names the exact grant an
        # admin has to make, and repeating the request will not help until
        # they do.
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.get(
    "/tasks/{task_id}/attachments",
    response_model=list[TaskAttachmentOut],
    dependencies=[Depends(require_module)],
    summary="The files on one of the caller's tasks",
)
async def task_attachments(
    task_id: str,
    request: Request,
    user: CurrentUser,
    roles: CurrentRoles,
    sp: Annotated[SharePointProposals, Depends(get_sharepoint)],
) -> list[TaskAttachmentOut]:
    task = await _authorised_task(task_id, user, roles, sp)
    try:
        files = await sp.attachments_of(task.id)
    except (SharePointConsentError, SharePointError) as exc:
        raise _translate_sp(exc) from exc

    base = str(request.url_for("download_task_attachment",
                               task_id=task.id, file_name="_")).rsplit("/", 1)[0]
    return [
        TaskAttachmentOut(
            file_name=f["file_name"],
            download_url=f"{base}/{quote(f['file_name'])}",
        )
        for f in files
    ]


@router.get(
    "/tasks/{task_id}/attachments/{file_name}",
    dependencies=[Depends(require_module)],
    summary="Download one file from one of the caller's tasks",
    response_class=Response,
    name="download_task_attachment",
)
async def download_task_attachment(
    task_id: str,
    file_name: str,
    user: CurrentUser,
    roles: CurrentRoles,
    sp: Annotated[SharePointProposals, Depends(get_sharepoint)],
) -> Response:
    task = await _authorised_task(task_id, user, roles, sp)
    try:
        content = await sp.attachment_content(task.id, file_name)
    except (SharePointConsentError, SharePointError) as exc:
        raise _translate_sp(exc) from exc
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers=download_headers(file_name),
    )


@router.post(
    "/tasks/{task_id}/attachments",
    response_model=list[TaskAttachmentOut],
    dependencies=[Depends(require_module)],
    status_code=status.HTTP_201_CREATED,
    summary="Attach a file to one of the caller's tasks",
)
async def upload_task_attachment(
    task_id: str,
    request: Request,
    user: CurrentUser,
    roles: CurrentRoles,
    sp: Annotated[SharePointProposals, Depends(get_sharepoint)],
    file: Annotated[UploadFile, File()],
) -> list[TaskAttachmentOut]:
    """Checked like every other upload — see ``app.hr.documents`` for the rules."""
    task = await _authorised_task(task_id, user, roles, sp)
    try:
        upload = accept(file.filename or "file", await file.read(), file.content_type)
    except UploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    try:
        await sp.add_attachment(task.id, upload.file_name, upload.content)
    except (SharePointConsentError, SharePointError) as exc:
        raise _translate_sp(exc) from exc
    logger.info("%s attached %r to task %s", user.email, upload.file_name, task.id)
    return await task_attachments(task_id, request, user, roles, sp)


@router.delete(
    "/tasks/{task_id}/attachments/{file_name}",
    dependencies=[Depends(require_module)],
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a file from one of the caller's tasks",
)
async def delete_task_attachment(
    task_id: str,
    file_name: str,
    user: CurrentUser,
    roles: CurrentRoles,
    sp: Annotated[SharePointProposals, Depends(get_sharepoint)],
) -> None:
    task = await _authorised_task(task_id, user, roles, sp)
    try:
        await sp.delete_attachment(task.id, file_name)
    except (SharePointConsentError, SharePointError) as exc:
        raise _translate_sp(exc) from exc
    logger.info("%s removed %r from task %s", user.email, file_name, task.id)


@router.patch(
    "/tasks/{task_id}",
    response_model=TaskOut,
    dependencies=[Depends(require_module)],
    summary="Edit one of the caller's tasks",
)
async def update_task(
    task_id: str,
    payload: TaskUpdateIn,
    user: CurrentUser,
    roles: CurrentRoles,
    sp: Annotated[SharePointProposals, Depends(get_sharepoint)],
) -> TaskOut:
    """Writes go to the live list — this is the one place the app writes to
    SharePoint, and it does so as the application identity. SharePoint's own
    "Modified By" will therefore say the app, not the person, which is why the
    person is recorded in our log here: it is the only record of who really
    made the change.
    """
    task = await _authorised_task(task_id, user, roles, sp)
    fields = payload.as_sharepoint_fields()
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Nothing to change"
        )
    try:
        updated = await sp.update_task(task.id, fields)
    except (SharePointConsentError, SharePointError) as exc:
        raise _translate_sp(exc) from exc
    logger.info(
        "%s updated task %s: %s", user.email, task.id, sorted(fields)
    )
    return TaskOut.from_domain(updated)
