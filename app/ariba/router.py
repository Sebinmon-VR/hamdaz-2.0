"""The Ariba tenders, read here — never from the portal on a request.

Listing reads the table the worker keeps. The only route that can open the
portal is the super admin's "visit now", and it goes through the same limits
as the loop: the pause after a refused sign-in and the daily cap still apply.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ariba import service
from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.models.ariba import AribaBcdFix
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles

router = APIRouter(prefix="/ariba", tags=["ariba"])

Session = Annotated[AsyncSession, Depends(get_session)]
Config = Annotated[Settings, Depends(get_settings)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can see or run the Ariba reader.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


@router.get("/events", summary="Tenders from the Ariba Events list, soonest closing first")
async def list_events(
    _: CurrentUser,
    session: Session,
    config: Config,
    status_: Annotated[str | None, Query(alias="status")] = None,
) -> list[dict[str, Any]]:
    return await service.events(session, config, status=status_)


@router.get("/status", summary="What the reader last did")
async def reader_status(_: SuperAdmin, session: Session) -> dict[str, Any]:
    record = await service.state(session)
    await session.commit()
    return {
        "last_visit_at": record.last_visit_at,
        "last_login_at": record.last_login_at,
        "last_result": record.last_result,
        "last_error": record.last_error,
        "paused_until": record.paused_until,
        "visits_today": record.visits_today if record.visits_on else 0,
        "watermark": record.watermark,
        "has_session": record.session_state is not None,
    }


@router.post("/visit", summary="Visit the portal now, within the usual limits")
async def visit_now(_: SuperAdmin, request: Request) -> dict[str, str]:
    worker = getattr(request.app.state, "ariba_worker", None)
    if worker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The Ariba reader is not running.")
    return {"result": await worker.tick(force=True)}


@router.get("/bcd", summary="BCD differences found, and the corrections written")
async def bcd_fixes(_: SuperAdmin, session: Session, config: Config) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(AribaBcdFix).order_by(AribaBcdFix.created_at.desc()).limit(200)
        )
    ).all()
    return {
        "writing": config.ariba_fix_bcd,
        "site_timezone": config.sharepoint_site_timezone,
        "rows": [
            {
                "id": str(r.id),
                "item_id": r.item_id,
                "doc_id": r.doc_id,
                "reference": r.reference,
                "task_title": r.task_title,
                "old_bcd": r.old_bcd,
                "new_bcd": r.new_bcd,
                "ariba_end_time": r.ariba_end_time,
                "applied": r.applied,
                "error": r.error,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@router.post("/bcd/check", summary="Compare BCD with the events held now — no portal visit")
async def bcd_check_now(_: SuperAdmin, request: Request) -> dict[str, str]:
    worker = getattr(request.app.state, "ariba_worker", None)
    if worker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The Ariba reader is not running.")
    return {"result": await worker.check_bcd_now()}
