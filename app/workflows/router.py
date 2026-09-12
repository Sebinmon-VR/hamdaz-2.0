"""Workflows over HTTP: starting, answering and watching runs; building flows.

Two routers. ``/workflows`` is for the people whose work it is — gated on the
``workflows`` module like proposals is gated on its module — and everything on
it is scoped to runs they own or their team's. ``/workflows/admin`` is the
builder and the switches, and is super admin only: a flow sends mail and
writes to Zoho on somebody's behalf, and deciding what it does is not a team
lead's call.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import service as access_service
from app.assistant import service as assistant_service
from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.hr.documents import UploadError, accept, download_headers
from app.models.user import User
from app.models.workflow import Workflow, WorkflowRun
from app.roles.catalogue import ADMIN_ROLES, SUPER_ADMIN
from app.roles.deps import CurrentRoles, has_any
from app.teams import service as teams_service
from app.workflows import catalogue, service
from app.workflows.engine import Services, WorkflowError
from app.workflows.schemas import (
    AnswerIn,
    BlockOut,
    BlocksOut,
    ConfigFieldOut,
    RunEventOut,
    RunFileOut,
    RunMessageOut,
    RunOut,
    RunStepOut,
    RunSummaryOut,
    SettingsIn,
    SettingsOut,
    StartRunIn,
    StepOut,
    TaskRunsOut,
    ToolChoiceOut,
    WorkflowIn,
    WorkflowOut,
    WorkflowPatch,
)
from app.workflows.service import WorkflowConflictError, WorkflowNotFoundError

logger = logging.getLogger("hamdaz.workflows")

MODULE_KEY = "workflows"

router = APIRouter(prefix="/workflows", tags=["workflows"])
admin_router = APIRouter(prefix="/workflows/admin", tags=["workflows admin"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


# ── guards ─────────────────────────────────────────────────────────────


async def require_module(user: CurrentUser, roles: CurrentRoles, session: Session) -> None:
    if not await access_service.can_reach(
        session, user_id=user.id, global_roles=roles, module_key=MODULE_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your team does not have the Workflows module. A super admin can grant it.",
        )


ModuleGate = Annotated[None, Depends(require_module)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin may change workflows.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


def _translate(exc: WorkflowError) -> HTTPException:
    if isinstance(exc, WorkflowNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, WorkflowConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def _services(request: Request, session: AsyncSession) -> Services:
    """Everything a step may need, from the app's state. Missing pieces are None."""
    state = request.app.state
    try:
        model_key = (await assistant_service.get_settings(session)).model_key
    except Exception:  # noqa: BLE001 - a flow with no agent step does not care
        model_key = ""
    return Services(
        settings=get_settings(),
        sharepoint=getattr(state, "sharepoint", None),
        mail=getattr(state, "mail_reader", None),
        zoho=getattr(state, "zoho", None),
        extractor=getattr(state, "quote_extractor", None),
        llm=getattr(state, "openai", None),
        executor=getattr(state, "assistant_executor", None),
        model_key=model_key,
    )


async def _my_team_ids(session: AsyncSession, user: User) -> list[uuid.UUID]:
    return [team.id for team, _ in await teams_service.teams_for_user(session, user.id)]


async def _visible_run(
    session: AsyncSession, run_id: uuid.UUID, user: User, roles: set[str]
) -> WorkflowRun:
    """The run, if this person may look at it: theirs, their team's, or they are an admin."""
    try:
        run = await service.get_run(session, run_id)
    except WorkflowError as exc:
        raise _translate(exc) from exc
    if run.owner_id == user.id or has_any(roles, ADMIN_ROLES):
        return run
    if run.team_id is not None and run.team_id in await _my_team_ids(session, user):
        return run
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run")


def _may_drive(run: WorkflowRun, user: User, roles: set[str]) -> None:
    """Answering, cancelling and retrying are the owner's, or an admin's."""
    if run.owner_id != user.id and not has_any(roles, ADMIN_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the person whose run this is can answer it.",
        )


# ── presenting ─────────────────────────────────────────────────────────


def _flow_out(flow: Workflow, open_runs: int = 0) -> WorkflowOut:
    return WorkflowOut(
        id=flow.id, key=flow.key, name=flow.name, description=flow.description,
        team_id=flow.team_id,
        team_slug=flow.team.slug if flow.team else None,
        team_name=flow.team.name if flow.team else None,
        subject_kind=flow.subject_kind, trigger=flow.trigger, enabled=flow.enabled,
        version=flow.version, is_system=flow.is_system, archived_at=flow.archived_at,
        steps=[StepOut(**s) for s in flow.steps or []],
        open_runs=open_runs,
    )


def _summary(run: WorkflowRun) -> RunSummaryOut:
    current = run.current_step
    return RunSummaryOut(
        id=run.id, workflow_id=run.workflow_id,
        workflow_key=run.workflow.key if run.workflow else "",
        workflow_name=run.workflow.name if run.workflow else "",
        subject_kind=run.subject_kind, subject_id=run.subject_id, subject_label=run.subject_label,
        owner_id=run.owner_id, owner_name=run.owner.display_name if run.owner else "",
        team_id=run.team_id, tag=run.tag, status=run.status, step_index=run.step_index,
        step_count=len(run.steps or []),
        current_step=str(current.get("name") or current.get("key")) if current else None,
        waiting_for=(run.pending or {}).get("title") if run.pending else None,
        started_at=run.started_at, finished_at=run.finished_at, error=run.error,
        cost_usd=run.cost_usd,
    )


def _run_out(run: WorkflowRun) -> RunOut:
    base = _summary(run).model_dump()
    return RunOut(
        **base,
        context={k: v for k, v in (run.context or {}).items() if not k.startswith("_")},
        pending=run.pending, wake_at=run.wake_at, deadline_at=run.deadline_at,
        steps=[RunStepOut(**s) for s in service.step_states(run)],
        events=[RunEventOut.model_validate(e) for e in run.events],
        files=[RunFileOut.model_validate(f) for f in run.files],
        messages=[RunMessageOut.model_validate(m) for m in run.messages],
    )


# ── the flows a person may start ───────────────────────────────────────


@router.get("", response_model=list[WorkflowOut], summary="Workflows I can start")
async def my_flows(user: CurrentUser, roles: CurrentRoles, session: Session, _: ModuleGate) -> list[WorkflowOut]:
    flows = await service.flows_for(session, user_id=user.id, is_admin=has_any(roles, ADMIN_ROLES))
    counts = await service.open_run_counts(session)
    return [_flow_out(f, counts.get(f.id, 0)) for f in flows]


@router.get("/runs", response_model=list[RunSummaryOut], summary="Runs I can see")
async def my_runs(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    _: ModuleGate,
    mine: Annotated[bool, Query(description="Only runs I own")] = False,
    open: Annotated[bool, Query(description="Only runs still going")] = False,
    workflow: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[RunSummaryOut]:
    """Mine and my teams'. An admin sees everybody's."""
    workflow_id = None
    if workflow:
        try:
            workflow_id = (await service.get_flow(session, workflow)).id
        except WorkflowError as exc:
            raise _translate(exc) from exc
    if has_any(roles, ADMIN_ROLES) and not mine:
        runs = await service.list_runs(session, workflow_id=workflow_id, open_only=open, limit=limit)
    elif mine:
        runs = await service.list_runs(session, owner_id=user.id, workflow_id=workflow_id, open_only=open, limit=limit)
    else:
        runs = await service.list_runs(
            session, owner_id=user.id, team_ids=await _my_team_ids(session, user),
            workflow_id=workflow_id, open_only=open, limit=limit,
        )
    return [_summary(r) for r in runs]


@router.get("/for-task/{task_id}", response_model=TaskRunsOut, summary="What a task can run, and has")
async def for_task(
    task_id: str, user: CurrentUser, roles: CurrentRoles, session: Session, _: ModuleGate
) -> TaskRunsOut:
    flows = await service.flows_for(session, user_id=user.id, is_admin=has_any(roles, ADMIN_ROLES))
    counts = await service.open_run_counts(session)
    runs = await service.list_runs(session, subject_id=task_id, limit=50)
    admin = has_any(roles, ADMIN_ROLES)
    mine = set(await _my_team_ids(session, user))
    visible = [r for r in runs if admin or r.owner_id == user.id or (r.team_id in mine)]
    return TaskRunsOut(
        workflows=[_flow_out(f, counts.get(f.id, 0)) for f in flows],
        runs=[_summary(r) for r in visible],
    )


# ── one run ────────────────────────────────────────────────────────────


@router.get("/runs/{run_id}", response_model=RunOut, summary="One run in full")
async def read_run(
    run_id: uuid.UUID, user: CurrentUser, roles: CurrentRoles, session: Session, _: ModuleGate
) -> RunOut:
    return _run_out(await _visible_run(session, run_id, user, roles))


@router.post(
    "/runs/{run_id}/answer", response_model=RunOut, summary="Answer what the run is waiting on"
)
async def answer_run(
    run_id: uuid.UUID,
    body: AnswerIn,
    request: Request,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    _: ModuleGate,
) -> RunOut:
    """A form's values, or a review's verdict, and the run carries on from there.

    The run continues in this request as far as it can — usually to the next
    question or the next wait — so the answer comes back with the run already
    moved on, and a person sees at once what their verification set off.
    """
    run = await _visible_run(session, run_id, user, roles)
    _may_drive(run, user, roles)
    values, value = body.merged()
    try:
        run = await service.answer(
            session, run, user=user, values=values, value=value,
            settings=await service.get_settings(session),
            services=await _services(request, session),
        )
    except WorkflowError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return _run_out(await service.get_run(session, run.id))


@router.post(
    "/runs/{run_id}/files",
    response_model=RunFileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a file to the question the run is asking",
)
async def upload_run_file(
    run_id: uuid.UUID,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    _: ModuleGate,
    file: Annotated[UploadFile, File()],
) -> RunFileOut:
    run = await _visible_run(session, run_id, user, roles)
    _may_drive(run, user, roles)
    try:
        upload = accept(file.filename or "file", await file.read(), file.content_type)
    except UploadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        row = await service.add_upload(
            session, run, file_name=upload.file_name, content=upload.content,
            content_type=upload.content_type,
        )
    except WorkflowError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return RunFileOut.model_validate(row)


@router.get(
    "/runs/{run_id}/files/{file_id}",
    summary="Download a file the run holds",
    response_class=Response,
)
async def download_run_file(
    run_id: uuid.UUID, file_id: uuid.UUID, user: CurrentUser, roles: CurrentRoles,
    session: Session, _: ModuleGate,
) -> Response:
    run = await _visible_run(session, run_id, user, roles)
    row = next((f for f in run.files if f.id == file_id), None)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such file")
    return Response(
        content=row.content,
        media_type=row.content_type or "application/octet-stream",
        headers=download_headers(row.file_name),
    )


@router.post("/runs/{run_id}/cancel", response_model=RunOut, summary="Stop a run")
async def cancel_run(
    run_id: uuid.UUID, user: CurrentUser, roles: CurrentRoles, session: Session, _: ModuleGate
) -> RunOut:
    run = await _visible_run(session, run_id, user, roles)
    _may_drive(run, user, roles)
    try:
        await service.cancel(session, run, user=user)
    except WorkflowError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return _run_out(await service.get_run(session, run.id))


@router.post("/runs/{run_id}/retry", response_model=RunOut, summary="Try the failed step again")
async def retry_run(
    run_id: uuid.UUID, request: Request, user: CurrentUser, roles: CurrentRoles,
    session: Session, _: ModuleGate,
) -> RunOut:
    run = await _visible_run(session, run_id, user, roles)
    _may_drive(run, user, roles)
    try:
        await service.retry(
            session, run, user=user, settings=await service.get_settings(session),
            services=await _services(request, session),
        )
    except WorkflowError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return _run_out(await service.get_run(session, run.id))


@router.post("/runs/{run_id}/wake", response_model=RunOut, summary="Check now rather than waiting")
async def wake_run(
    run_id: uuid.UUID, request: Request, user: CurrentUser, roles: CurrentRoles,
    session: Session, _: ModuleGate,
) -> RunOut:
    """Look for the reply or the approval now, instead of at the next poll."""
    from app.workflows.engine import advance

    run = await _visible_run(session, run_id, user, roles)
    _may_drive(run, user, roles)
    if run.status != "waiting_event":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The run is not waiting on anything outside.")
    await advance(session, run, await service.get_settings(session), await _services(request, session))
    await session.commit()
    return _run_out(await service.get_run(session, run.id))


# ── starting one ───────────────────────────────────────────────────────


@router.post(
    "/{key}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED,
    summary="Start a workflow on a task",
)
async def start_run(
    key: str,
    body: StartRunIn,
    request: Request,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    _: ModuleGate,
) -> RunOut:
    """Begin, and run as far as the first question.

    The run is the caller's: every route it calls it calls as them, every
    question it asks it asks them. A flow tied to a team can be started by
    the team's members and by admins.
    """
    try:
        flow = await service.get_flow(session, key)
    except WorkflowError as exc:
        raise _translate(exc) from exc
    allowed = await service.flows_for(session, user_id=user.id, is_admin=has_any(roles, ADMIN_ROLES))
    if flow.id not in {f.id for f in allowed}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="That workflow is not your team's.")
    try:
        run = await service.start(
            session, flow, owner=user, subject_id=body.subject_id, subject_label=body.subject_label,
            settings=await service.get_settings(session), services=await _services(request, session),
        )
    except WorkflowError as exc:
        raise _translate(exc) from exc
    await session.commit()
    logger.info("%s started %s on %s as %s", user.email, flow.key, body.subject_id, run.tag)
    return _run_out(await service.get_run(session, run.id))


@router.get("/{key}", response_model=WorkflowOut, summary="One workflow")
async def read_flow(key: str, user: CurrentUser, roles: CurrentRoles, session: Session, _: ModuleGate) -> WorkflowOut:
    try:
        flow = await service.get_flow(session, key)
    except WorkflowError as exc:
        raise _translate(exc) from exc
    counts = await service.open_run_counts(session)
    return _flow_out(flow, counts.get(flow.id, 0))


# ── administration ─────────────────────────────────────────────────────


@admin_router.get("/settings", response_model=SettingsOut, summary="The switches")
async def read_settings(admin: SuperAdmin, session: Session) -> SettingsOut:
    return SettingsOut.model_validate(await service.get_settings(session))


@admin_router.patch("/settings", response_model=SettingsOut, summary="Flip a switch")
async def update_settings(body: SettingsIn, admin: SuperAdmin, session: Session) -> SettingsOut:
    row = await service.update_settings(
        session, actor_id=admin.id, changes=body.model_dump(exclude_unset=True)
    )
    await session.commit()
    logger.info("%s changed workflow settings: %s", admin.email, body.model_dump(exclude_unset=True))
    return SettingsOut.model_validate(row)


@admin_router.get("/blocks", response_model=BlocksOut, summary="The blocks a flow is built from")
async def blocks(admin: SuperAdmin) -> BlocksOut:
    return BlocksOut(
        blocks=[
            BlockOut(
                kind=b.kind, name=b.name, description=b.description, waits=b.waits, switch=b.switch,
                fields=[ConfigFieldOut(**f) for f in b.config_schema()],
            )
            for b in catalogue.BLOCKS
        ],
        tools=[ToolChoiceOut(**t) for t in catalogue.tool_choices()],
        schemas=sorted(catalogue.SCHEMAS),
    )


@admin_router.get("/flows", response_model=list[WorkflowOut], summary="Every workflow, archived included")
async def all_flows(admin: SuperAdmin, session: Session) -> list[WorkflowOut]:
    counts = await service.open_run_counts(session)
    return [_flow_out(f, counts.get(f.id, 0)) for f in await service.list_flows(session, include_archived=True)]


@admin_router.post(
    "/flows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED, summary="Create a workflow"
)
async def create_flow(body: WorkflowIn, admin: SuperAdmin, session: Session) -> WorkflowOut:
    payload: dict[str, Any] = body.model_dump()
    payload["steps"] = [s.model_dump() for s in body.steps]
    try:
        flow = await service.create_flow(session, payload=payload, actor=admin)
    except (WorkflowError, catalogue.StepError) as exc:
        raise (_translate(exc) if isinstance(exc, WorkflowError) else HTTPException(400, str(exc))) from exc
    await session.commit()
    return _flow_out(await service.get_flow(session, flow.id))


@admin_router.get("/flows/{key}", response_model=WorkflowOut, summary="One workflow, for editing")
async def read_flow_admin(key: str, admin: SuperAdmin, session: Session) -> WorkflowOut:
    try:
        flow = await service.get_flow(session, key)
    except WorkflowError as exc:
        raise _translate(exc) from exc
    counts = await service.open_run_counts(session)
    return _flow_out(flow, counts.get(flow.id, 0))


@admin_router.patch("/flows/{key}", response_model=WorkflowOut, summary="Change a workflow")
async def update_flow(key: str, body: WorkflowPatch, admin: SuperAdmin, session: Session) -> WorkflowOut:
    try:
        flow = await service.get_flow(session, key)
        changes = body.model_dump(exclude_unset=True)
        if changes.get("steps") is not None:
            changes["steps"] = [s.model_dump() for s in body.steps or []]
        flow = await service.update_flow(session, flow, changes=changes, actor=admin)
    except WorkflowError as exc:
        raise _translate(exc) from exc
    except catalogue.StepError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await session.commit()
    return _flow_out(await service.get_flow(session, flow.id))


@admin_router.post("/flows/{key}/archive", response_model=WorkflowOut, summary="Retire a workflow")
async def archive_flow(key: str, admin: SuperAdmin, session: Session) -> WorkflowOut:
    try:
        flow = await service.archive_flow(session, await service.get_flow(session, key))
    except WorkflowError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return _flow_out(flow)


@admin_router.post("/flows/{key}/restore", response_model=WorkflowOut, summary="Bring one back")
async def restore_flow(key: str, admin: SuperAdmin, session: Session) -> WorkflowOut:
    try:
        flow = await service.restore_flow(session, await service.get_flow(session, key))
    except WorkflowError as exc:
        raise _translate(exc) from exc
    await session.commit()
    return _flow_out(flow)


@admin_router.delete("/flows/{key}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a workflow")
async def delete_flow(key: str, admin: SuperAdmin, session: Session) -> None:
    try:
        await service.delete_flow(session, await service.get_flow(session, key))
    except WorkflowError as exc:
        raise _translate(exc) from exc
    await session.commit()


__all__ = ["router", "admin_router", "quote"]
