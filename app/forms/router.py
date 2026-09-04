"""Form templates: what a form asks for, editable by a super admin.

**Only a super admin writes.** Creating, editing, publishing, archiving,
deleting and granting are all theirs — a template decides what the business
records, and letting each team adjust the form they fill in produces records
that cannot be compared with each other.

**Everybody reads and uses**, subject to the grants: which team, and what
standing inside it. ``GET /templates/usable`` answers "what can I actually fill
in", which is the only question most callers have.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.forms import service
from app.forms.schemas import (
    GrantIn,
    GrantOut,
    TemplateIn,
    TemplateOut,
    TemplateSummaryOut,
    TemplateUpdateIn,
)
from app.forms.service import (
    TemplateError,
    TemplateNotFoundError,
    TemplatePermissionError,
)
from app.models.templates import FormTemplate
from app.roles.deps import CurrentRoles

router = APIRouter(prefix="/templates", tags=["form templates"])

Session = Annotated[AsyncSession, Depends(get_session)]


def _translate(exc: TemplateError) -> HTTPException:
    if isinstance(exc, TemplateNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, TemplatePermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def _out(
    session: AsyncSession, template: FormTemplate, *, user, roles: set[str]
) -> TemplateOut:
    body = TemplateOut.model_validate(template)
    body.created_by_name = (
        template.created_by.display_name if template.created_by else None
    )
    body.grants = [
        GrantOut(
            team_id=g.team_id,
            team_name=g.team.name if g.team else None,
            allowed_roles=list(g.allowed_roles or []),
            note=g.note,
        )
        for g in template.grants
    ]
    usable = await service.may_use(session, template, user=user, roles=roles)
    body.may_use = usable.allowed
    body.use_reason = usable.reason
    body.may_edit = bool(roles & service.TEMPLATE_ADMINS)
    return body


def _summary(template: FormTemplate) -> TemplateSummaryOut:
    return TemplateSummaryOut(
        id=template.id,
        key=template.key,
        name=template.name,
        kind=template.kind,
        status=template.status,
        version=template.version,
        field_count=len(template.fields or []),
        grant_count=len(template.grants or []),
        description=template.description,
    )


async def _load(session: AsyncSession, ref: str) -> FormTemplate:
    try:
        return await service.get(session, ref)
    except TemplateError as exc:
        raise _translate(exc) from exc


# ── reading ────────────────────────────────────────────────────────────


@router.get("", response_model=list[TemplateSummaryOut], summary="Every template")
async def index(
    _: CurrentUser,
    session: Session,
    kind: Annotated[str | None, Query(description="Only this kind of form")] = None,
    include_archived: Annotated[bool, Query()] = False,
) -> list[TemplateSummaryOut]:
    rows = await service.all_templates(
        session, include_archived=include_archived, kind=kind
    )
    return [_summary(t) for t in rows]


@router.get(
    "/usable",
    response_model=list[TemplateSummaryOut],
    summary="Templates I can actually fill in",
)
async def usable(
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
    kind: Annotated[str | None, Query()] = None,
) -> list[TemplateSummaryOut]:
    """The only question most callers have."""
    rows = await service.usable_by(session, user=user, roles=roles, kind=kind)
    return [_summary(t) for t in rows]


@router.get("/{ref}", response_model=TemplateOut, summary="One template in full")
async def detail(
    ref: str, user: CurrentUser, roles: CurrentRoles, session: Session
) -> TemplateOut:
    """The fields, in order, with their sections and Zoho mappings.

    ``may_use`` says whether you may fill it in, and ``use_reason`` says why not
    when you may not.
    """
    return await _out(session, await _load(session, ref), user=user, roles=roles)


# ── writing: super admin only ──────────────────────────────────────────


@router.post(
    "",
    response_model=TemplateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a template",
)
async def create(
    payload: TemplateIn, user: CurrentUser, roles: CurrentRoles, session: Session
) -> TemplateOut:
    """Created as a draft, and granted to nobody.

    Both are deliberate: a half-written form should not be fillable, and a form
    should appear for a team because somebody decided it should rather than
    because it was saved.
    """
    try:
        template = await service.create(
            session,
            roles=roles,
            actor=user,
            key=payload.key,
            name=payload.name,
            kind=payload.kind or payload.key,
            fields=[f.model_dump(exclude_none=True) for f in payload.fields],
            sections=[s.model_dump(exclude_none=True) for s in payload.sections],
            description=payload.description,
        )
    except TemplateError as exc:
        raise _translate(exc) from exc
    return await _out(session, template, user=user, roles=roles)


@router.patch("/{ref}", response_model=TemplateOut, summary="Edit a template")
async def update(
    ref: str,
    payload: TemplateUpdateIn,
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
) -> TemplateOut:
    template = await _load(session, ref)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    if payload.fields is not None:
        changes["fields"] = [f.model_dump(exclude_none=True) for f in payload.fields]
    if payload.sections is not None:
        changes["sections"] = [s.model_dump(exclude_none=True) for s in payload.sections]
    try:
        await service.update(session, template, roles=roles, actor=user, **changes)
    except TemplateError as exc:
        raise _translate(exc) from exc
    return await _out(session, template, user=user, roles=roles)


@router.post("/{ref}/publish", response_model=TemplateOut, summary="Make it usable")
async def publish(
    ref: str, user: CurrentUser, roles: CurrentRoles, session: Session
) -> TemplateOut:
    template = await _load(session, ref)
    try:
        await service.publish(session, template, roles=roles, actor=user)
    except TemplateError as exc:
        raise _translate(exc) from exc
    return await _out(session, template, user=user, roles=roles)


@router.post("/{ref}/archive", response_model=TemplateOut, summary="Retire a template")
async def archive(
    ref: str, user: CurrentUser, roles: CurrentRoles, session: Session
) -> TemplateOut:
    """Kept readable. A form somebody submitted last month is unreadable if the
    definition behind it has gone."""
    template = await _load(session, ref)
    try:
        await service.archive(session, template, roles=roles, actor=user)
    except TemplateError as exc:
        raise _translate(exc) from exc
    return await _out(session, template, user=user, roles=roles)


@router.post("/{ref}/restore", response_model=TemplateOut, summary="Bring one back")
async def restore(
    ref: str, user: CurrentUser, roles: CurrentRoles, session: Session
) -> TemplateOut:
    template = await _load(session, ref)
    try:
        await service.restore(session, template, roles=roles, actor=user)
    except TemplateError as exc:
        raise _translate(exc) from exc
    return await _out(session, template, user=user, roles=roles)


@router.delete(
    "/{ref}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a template"
)
async def remove(ref: str, _: CurrentUser, roles: CurrentRoles, session: Session) -> None:
    """Only one nothing has been filled in from. Anything else is archived."""
    template = await _load(session, ref)
    try:
        await service.delete(session, template, roles=roles)
    except TemplateError as exc:
        raise _translate(exc) from exc


# ── who may use it ─────────────────────────────────────────────────────


@router.put(
    "/{ref}/grants",
    response_model=TemplateOut,
    summary="Set which teams and roles may use it",
)
async def set_grants(
    ref: str,
    payload: list[GrantIn],
    user: CurrentUser,
    roles: CurrentRoles,
    session: Session,
) -> TemplateOut:
    """Two independent questions: which team, and what standing inside it.

    Leave ``team_id`` out for every team; leave ``allowed_roles`` empty for
    anyone on the team. An empty list of grants means nobody but a super admin.
    """
    template = await _load(session, ref)
    try:
        await service.set_grants(
            session,
            template,
            roles=roles,
            grants=[g.model_dump() for g in payload],
        )
    except TemplateError as exc:
        raise _translate(exc) from exc
    return await _out(session, template, user=user, roles=roles)


@router.get(
    "/{ref}/grants", response_model=list[GrantOut], summary="Who may use it"
)
async def grants(ref: str, _: CurrentUser, session: Session) -> list[GrantOut]:
    template = await _load(session, ref)
    return [
        GrantOut(
            team_id=g.team_id,
            team_name=g.team.name if g.team else None,
            allowed_roles=list(g.allowed_roles or []),
            note=g.note,
        )
        for g in template.grants
    ]
