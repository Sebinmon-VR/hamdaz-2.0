"""User labels: the vocabulary the assignment policy speaks in.

Reading is open to any signed-in user — who is senior, who is on leave and who
is new is ordinary team information, and hiding it would only make the
assignment policy look arbitrary to the people it applies to.

Writing is not. Creating labels and handing them out changes how work is shared,
so it needs a super admin, the CEO or a manager — the same three who may edit the
policy itself.

Two labels cannot be given out here at all. ``on-leave`` and ``new-joiner`` are
worked out from approved leave and from joining dates, so the way to change them
is to change those, not to argue with the label.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.labels import service
from app.labels.catalogue import BY_KEY
from app.labels.schemas import (
    AssignLabelIn,
    HeldLabelOut,
    JoinedOnIn,
    LabelIn,
    LabelOut,
    LabelUpdateIn,
    PersonLabelsOut,
)
from app.labels.service import LabelError, LabelNotFoundError
from app.models.labels import DERIVED_KEYS
from app.models.user import User
from app.roles.deps import CurrentRoles

router = APIRouter(prefix="/labels", tags=["labels"])

Session = Annotated[AsyncSession, Depends(get_session)]

#: The same three who may edit the assignment policy. Labels and policy are two
#: halves of one decision, so splitting who may change them would be a gap.
EDITORS = frozenset({"super_admin", "ceo", "manager"})


def _translate(exc: LabelError) -> HTTPException:
    if isinstance(exc, LabelNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def require_editor(roles: CurrentRoles) -> None:
    if not roles & EDITORS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only a super admin, the CEO or a manager may change labels — "
                "they decide how work is shared out."
            ),
        )


Editor = Annotated[None, Depends(require_editor)]


def _out(label) -> LabelOut:
    body = LabelOut.model_validate(label)
    body.derived = label.key in DERIVED_KEYS
    return body


@router.get("", response_model=list[LabelOut], summary="The label catalogue")
async def index(
    _: CurrentUser,
    session: Session,
    team: Annotated[uuid.UUID | None, Query(description="Include this team's own labels")] = None,
) -> list[LabelOut]:
    return [_out(label) for label in await service.all_labels(session, team_id=team)]


@router.post(
    "",
    response_model=LabelOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a label",
)
async def create(
    payload: LabelIn,
    _user: CurrentUser,
    _editor: Editor,
    session: Session,
    team: Annotated[uuid.UUID | None, Query(description="Scope it to one team")] = None,
) -> LabelOut:
    try:
        label = await service.create_label(
            session,
            key=payload.key,
            name=payload.name,
            kind=payload.kind,
            description=payload.description,
            color=payload.color,
            team_id=team,
        )
    except LabelError as exc:
        raise _translate(exc) from exc
    return _out(label)


@router.patch("/{key}", response_model=LabelOut, summary="Rename or restyle a label")
async def update(
    key: str,
    payload: LabelUpdateIn,
    _user: CurrentUser,
    _editor: Editor,
    session: Session,
    team: Annotated[uuid.UUID | None, Query()] = None,
) -> LabelOut:
    """Change the wording, not the identity.

    The ``key`` cannot be edited: the assignment policy, every assignment and
    every stored run refer to it, and changing it would detach all of them at
    once. This is what the delete refusal on a system label points you at.
    """
    try:
        label = await service.update_label(
            session,
            await service.get_label(session, key, team_id=team),
            **payload.model_dump(exclude_unset=True),
        )
    except LabelError as exc:
        raise _translate(exc) from exc
    return _out(label)


@router.delete(
    "/{key}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a label"
)
async def remove(
    key: str,
    _user: CurrentUser,
    _editor: Editor,
    session: Session,
    team: Annotated[uuid.UUID | None, Query()] = None,
) -> None:
    try:
        await service.delete_label(session, await service.get_label(session, key, team_id=team))
    except LabelError as exc:
        raise _translate(exc) from exc


# ── who holds what ─────────────────────────────────────────────────────


@router.get(
    "/people",
    response_model=list[PersonLabelsOut],
    summary="Everyone's labels, including the automatic ones",
)
async def people(
    _: CurrentUser,
    session: Session,
    team: Annotated[
        uuid.UUID | None,
        Query(description="Judge team-scoped labels against this team"),
    ] = None,
    new_joiner_days: Annotated[int, Query(ge=0, le=3650)] = 90,
) -> list[PersonLabelsOut]:
    """Labels as they apply *today*.

    Includes the two nobody assigns: someone on approved leave shows ``on-leave``
    until the day their leave ends, and a recent starter shows ``new-joiner``
    until the window passes. Neither is stored, so neither can be stale.
    """
    users = list(
        (
            await session.scalars(
                select(User).where(User.is_active.is_(True)).order_by(User.display_name)
            )
        ).all()
    )
    held = await service.effective_labels(
        session, users, team_id=team, new_joiner_days=new_joiner_days
    )
    return [
        PersonLabelsOut(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email,
            joined_on=user.joined_on,
            labels=[HeldLabelOut(**vars(label)) for label in held.get(user.id, [])],
        )
        for user in users
    ]


@router.post("/assign", response_model=PersonLabelsOut, summary="Give someone a label")
async def assign(
    payload: AssignLabelIn,
    actor: CurrentUser,
    _editor: Editor,
    session: Session,
) -> PersonLabelsOut:
    if payload.label_key in DERIVED_KEYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"{payload.label_key!r} is worked out automatically — from approved "
                f"leave, or from the joining date. Change those instead."
            ),
        )
    try:
        user = await _user(session, payload.user_id)
        label = await service.get_label(session, payload.label_key, team_id=None)
        await service.assign(
            session,
            user=user,
            label=label,
            actor=actor,
            team_id=payload.team_id,
            expires_at=payload.expires_at,
            note=payload.note,
        )
    except LabelError as exc:
        raise _translate(exc) from exc
    return await _person(session, user, team_id=payload.team_id)


@router.post("/unassign", response_model=PersonLabelsOut, summary="Take a label away")
async def unassign(
    payload: AssignLabelIn, _user_: CurrentUser, _editor: Editor, session: Session
) -> PersonLabelsOut:
    try:
        user = await _user(session, payload.user_id)
        label = await service.get_label(session, payload.label_key, team_id=None)
        await service.unassign(session, user=user, label=label, team_id=payload.team_id)
    except LabelError as exc:
        raise _translate(exc) from exc
    return await _person(session, user, team_id=payload.team_id)


@router.put(
    "/people/{user_id}/joined-on",
    response_model=PersonLabelsOut,
    summary="Set the date the new-joiner rule counts from",
)
async def set_joined_on(
    user_id: uuid.UUID,
    payload: JoinedOnIn,
    _user_: CurrentUser,
    _editor: Editor,
    session: Session,
) -> PersonLabelsOut:
    """Entra does not give us a hire date, so this is where a real one goes.

    Left unset, the new-joiner rule falls back to when the person first appeared
    in this system — usually close, occasionally very wrong for somebody who was
    here long before the ERP was.
    """
    user = await _user(session, user_id)
    user.joined_on = payload.joined_on
    await session.flush()
    return await _person(session, user)


async def _user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise LabelNotFoundError("No such person")
    return user


async def _person(
    session: AsyncSession, user: User, *, team_id: uuid.UUID | None = None
) -> PersonLabelsOut:
    held = await service.effective_labels(session, [user], team_id=team_id)
    return PersonLabelsOut(
        user_id=user.id,
        display_name=user.display_name,
        email=user.email,
        joined_on=user.joined_on,
        labels=[HeldLabelOut(**vars(label)) for label in held.get(user.id, [])],
    )


@router.get(
    "/suggested",
    response_model=list[dict],
    summary="The shipped labels and their suggested capacities",
)
async def suggested(_: CurrentUser) -> list[dict]:
    """What a policy is seeded with, so a screen can explain the defaults."""
    return [
        {
            "key": spec.key,
            "name": spec.name,
            "kind": spec.kind,
            "capacity": spec.capacity,
            "derived": spec.derived,
            "description": spec.description,
        }
        for spec in BY_KEY.values()
    ]
