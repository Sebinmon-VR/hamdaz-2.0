"""Everything known about one person, in one call — and how to undo it.

``GET /users/{ref}`` gathers every registered section concurrently. As modules
are added their data appears here automatically, so a caller never has to fan
out across endpoints itself.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.profiles.sections  # noqa: F401 — importing registers the sections
from app.auth.deps import CurrentUser
from app.core.db import get_session, get_session_factory
from app.directory.router import get_directory
from app.models.user import User
from app.profiles import service
from app.profiles.registry import all_sections
from app.profiles.schemas import ResetResult, SectionInfo, UserProfileOut
from app.profiles.service import ProfileError
from app.roles.deps import AdminUser

router = APIRouter(prefix="/users", tags=["users"])

Session = Annotated[AsyncSession, Depends(get_session)]
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Directory = Annotated[object, Depends(get_directory)]


async def _find(session: AsyncSession, ref: str) -> User:
    """By local id, Entra object id, or email — whichever the caller has."""
    import uuid as _uuid

    try:
        as_uuid: _uuid.UUID | None = _uuid.UUID(ref)
    except ValueError:
        as_uuid = None

    user = None
    if as_uuid is not None:
        user = await session.get(User, as_uuid)
    if user is None:
        user = await session.scalar(select(User).where(User.entra_object_id == ref))
    if user is None and "@" in ref:
        user = await session.scalar(select(User).where(User.email == ref.lower()))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No user matching {ref!r}. They may not have been added yet.",
        )
    return user


@router.get("/sections", response_model=list[SectionInfo], summary="What a profile can contain")
async def list_sections(_: CurrentUser) -> list[SectionInfo]:
    # Lets a client discover new modules' data without being redeployed.
    return [
        SectionInfo(
            key=s.key, label=s.label, remote=s.remote, heavy=s.heavy, resettable=s.purge is not None
        )
        for s in all_sections()
    ]


@router.get("/{ref}", response_model=UserProfileOut, summary="Everything about one user")
async def get_profile(
    ref: str,
    _: CurrentUser,
    session: Session,
    factory: Factory,
    directory: Directory,
    include: Annotated[
        str | None,
        Query(description="Comma-separated section keys. Omit for all of them."),
    ] = None,
    local_only: Annotated[
        bool, Query(description="Skip sections that call out to Entra")
    ] = False,
) -> UserProfileOut:
    user = await _find(session, ref)

    keys = [k.strip() for k in include.split(",") if k.strip()] if include else None
    try:
        profile = await service.load_profile(
            user,
            factory=factory,
            directory=directory,
            include=keys,
            include_remote=not local_only,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc).strip("'")
        ) from exc
    return UserProfileOut(**profile)


@router.post(
    "/{ref}/reset",
    response_model=ResetResult,
    summary="Strip every module's data, keeping the account",
)
async def reset_user(
    ref: str, actor: AdminUser, session: Session, factory: Factory, directory: Directory
) -> ResetResult:
    user = await _find(session, ref)
    # Snapshot first: once it is gone there is nothing to report on, and an
    # admin doing this deserves to see what they removed.
    before = await service.load_profile(
        user, factory=factory, directory=directory, include_remote=False
    )

    try:
        removed = await service.reset_user(session, user=user, actor_id=actor.id)
    except ProfileError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return ResetResult(
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        account_deleted=False,
        removed=removed,
        previous=before["sections"],
    )


@router.delete(
    "/{ref}",
    response_model=ResetResult,
    summary="Remove the user from the ERP entirely",
)
async def purge_user(
    ref: str, actor: AdminUser, session: Session, factory: Factory, directory: Directory
) -> ResetResult:
    user = await _find(session, ref)
    before = await service.load_profile(
        user, factory=factory, directory=directory, include_remote=False
    )
    identity = (user.id, user.email, user.display_name)

    try:
        removed = await service.purge_user(session, user=user, actor_id=actor.id)
    except ProfileError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return ResetResult(
        user_id=str(identity[0]),
        email=identity[1],
        display_name=identity[2],
        account_deleted=True,
        removed=removed,
        previous=before["sections"],
    )
