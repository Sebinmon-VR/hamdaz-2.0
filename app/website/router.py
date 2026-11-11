"""Website enquiries, for the people who answer them.

Gated by the ``website_enquiries`` module on the router itself, so a route added
later cannot forget it. Super admins reach it as they reach everything; the CEO
and managers by their role (``ROLE_MODULES``); anybody else through a team a
super admin granted it to — Sales, typically.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.deps import module_guard
from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.models.user import User
from app.models.website import WebsiteEnquiry, WebsiteEnquiryStatus
from app.website import MODULE_KEY, MODULE_NAME, service
from app.website.schemas import (
    EnquiryOut,
    EnquiryPage,
    EnquirySummaryOut,
    EnquiryUpdateIn,
    PersonOut,
)

router = APIRouter(
    prefix="/website-enquiries",
    tags=["website enquiries"],
    dependencies=[Depends(module_guard(MODULE_KEY, MODULE_NAME))],
)

Session = Annotated[AsyncSession, Depends(get_session)]


def _person(user: User | None) -> PersonOut | None:
    if user is None:
        return None
    return PersonOut(id=user.id, name=user.display_name, email=user.email)


def _preview(message: str) -> str:
    flat = " ".join(message.split())
    return flat if len(flat) <= 160 else f"{flat[:157].rstrip()}…"


def _summary(row: WebsiteEnquiry) -> dict:
    return {
        "id": row.id,
        "created_at": row.created_at,
        "name": row.name,
        "company": row.company,
        "email": row.email,
        "phone": row.phone,
        "topics": list(row.topics or []),
        "preview": _preview(row.message),
        "status": row.status,
        "assigned_to": _person(row.assigned_to),
    }


def _full(row: WebsiteEnquiry) -> EnquiryOut:
    return EnquiryOut(
        **_summary(row),
        message=row.message,
        page=row.page,
        landing=row.landing,
        referrer=row.referrer,
        attribution=dict(row.attribution or {}),
        notes=row.notes,
        handled_at=row.handled_at,
        handled_by=_person(row.handled_by),
        updated_at=row.updated_at,
    )


async def _load(session: AsyncSession, enquiry_id: uuid.UUID) -> WebsiteEnquiry:
    try:
        return await service.get(session, enquiry_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("", response_model=EnquiryPage, summary="Enquiries, newest first")
async def index(
    session: Session,
    _user: CurrentUser,
    status: WebsiteEnquiryStatus | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EnquiryPage:
    rows, total, counts = await service.listing(
        session, status=status, search=q, limit=limit, offset=offset
    )
    return EnquiryPage(
        enquiries=[EnquirySummaryOut(**_summary(r)) for r in rows], total=total, counts=counts
    )


@router.get("/people", response_model=list[PersonOut], summary="Who an enquiry can be given to")
async def people(session: Session, _user: CurrentUser) -> list[PersonOut]:
    """Everyone who can open this module — nobody else could take one on."""
    return [p for u in await service.people_who_can_see(session) if (p := _person(u))]


@router.get("/{enquiry_id}", response_model=EnquiryOut, summary="One enquiry")
async def detail(enquiry_id: uuid.UUID, session: Session, _user: CurrentUser) -> EnquiryOut:
    return _full(await _load(session, enquiry_id))


@router.patch("/{enquiry_id}", response_model=EnquiryOut, summary="Take it on, move it along")
async def change(
    enquiry_id: uuid.UUID, body: EnquiryUpdateIn, session: Session, user: CurrentUser
) -> EnquiryOut:
    row = await _load(session, enquiry_id)
    try:
        row = await service.update(
            session, row, changes=body.model_dump(include=body.model_fields_set), by=user
        )
    except service.EnquiryError as exc:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _full(row)
