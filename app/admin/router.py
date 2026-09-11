"""One call that returns everything a super admin needs to draw the new screens.

A frontend building the administration section should not have to make eleven
requests to find out whether anything needs attention. This returns the
sections, what each one currently says, the permission rules behind them, and
every settings row — in one payload, shaped so a dashboard can be rendered
without knowing which module owns what.

Read-only. Everything here is available individually through the modules' own
routes, which are where changes are made; this is the index and the overview.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.catalogue import PERMISSION_RULES, SECTIONS
from app.admin.schemas import (
    ConsoleOut,
    EndpointOut,
    PermissionRuleOut,
    RoleHolderOut,
    SectionOut,
)
from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.models.intake import IntakeMessage, IntakeSettings, IntakeStatus
from app.models.notification import Notification
from app.models.proposal_index import MirrorState, ProposalIndexItem
from app.models.report import (
    DeliveryStatus,
    Report,
    ReportDelivery,
    ReportSchedule,
    ReportSettings,
    ReportStatus,
)
from app.models.role import Role, RoleScope, UserRole
from app.models.user import User
from app.roles.catalogue import SUPER_ADMIN
from app.roles.deps import CurrentRoles

router = APIRouter(prefix="/admin", tags=["administration"])

Session = Annotated[AsyncSession, Depends(get_session)]


async def require_super_admin(user: CurrentUser, roles: CurrentRoles) -> User:
    if SUPER_ADMIN not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super admin can see the administration console.",
        )
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


async def _count(session: AsyncSession, statement) -> int:
    return int(await session.scalar(statement) or 0)


async def _intake_status(session: AsyncSession) -> dict[str, Any]:
    settings = await session.get(IntakeSettings, 1)
    counts = {
        row.status: int(row.n)
        for row in (
            await session.execute(
                select(IntakeMessage.status, func.count().label("n")).group_by(
                    IntakeMessage.status
                )
            )
        ).all()
    }
    return {
        "enabled": bool(settings and settings.enabled),
        "mailbox": settings.mailbox if settings else "",
        "senders": len(settings.allowed_senders or []) if settings else 0,
        # Surfaced first and named plainly. It is the one setting here that
        # changes something outside this system.
        "writes_to_sharepoint": bool(settings and settings.create_in_sharepoint),
        "teams_webhook_set": bool(settings and settings.teams_webhook_url),
        "subscription_expires_at": (
            settings.subscription_expires_at if settings else None
        ),
        "last_poll_at": settings.last_poll_at if settings else None,
        "last_error": settings.last_error if settings else None,
        "messages": {s.value: counts.get(s.value, 0) for s in IntakeStatus},
        # The number that would worry somebody, hoisted out of the breakdown.
        "needs_attention": counts.get(IntakeStatus.FAILED.value, 0),
    }


async def _mirror_status(session: AsyncSession) -> dict[str, Any]:
    state = await session.get(MirrorState, 1)
    rows = await _count(
        session,
        select(func.count())
        .select_from(ProposalIndexItem)
        .where(ProposalIndexItem.deleted.is_(False)),
    )
    embedded = await _count(
        session,
        select(func.count())
        .select_from(ProposalIndexItem)
        .where(
            ProposalIndexItem.deleted.is_(False),
            ProposalIndexItem.embedding.is_not(None),
        ),
    )
    stale = None
    if state and state.last_sync_at:
        stale = int((datetime.now(UTC) - state.last_sync_at).total_seconds())
    return {
        "rows": rows,
        "embedded": embedded,
        # Below one, matching is running without its middle stage — usually a
        # missing OpenAI key rather than anything broken.
        "embedding_coverage": round(embedded / rows, 3) if rows else 0.0,
        "last_sync_at": state.last_sync_at if state else None,
        "seconds_since_sync": stale,
        "last_duration_ms": state.duration_ms if state else 0,
        "rows_changed_last": state.rows_changed if state else 0,
        "rows_embedded_last": state.rows_embedded if state else 0,
        "last_error": state.last_error if state else None,
        "needs_attention": 1 if (state and state.last_error) or rows == 0 else 0,
    }


async def _standing_status(session: AsyncSession) -> dict[str, Any]:
    from app.models.analytics import LiveScore

    ranked = await _count(
        session,
        select(func.count()).select_from(LiveScore).where(LiveScore.rank > 0),
    )
    excluded = await _count(
        session,
        select(func.count()).select_from(LiveScore).where(LiveScore.eligible.is_(False)),
    )
    newest = await session.scalar(select(func.max(LiveScore.computed_at)))
    top = (
        await session.scalars(
            select(LiveScore)
            .where(LiveScore.team_id.is_(None), LiveScore.rank == 1)
            .limit(1)
        )
    ).first()
    return {
        "ranked": ranked,
        "excluded": excluded,
        "computed_at": newest,
        "next_up": top.display_name if top else None,
        "needs_attention": 1 if ranked == 0 else 0,
    }


async def _reports_status(session: AsyncSession) -> dict[str, Any]:
    settings = await session.get(ReportSettings, 1)
    week = datetime.now(UTC) - timedelta(days=7)
    failed = await _count(
        session,
        select(func.count())
        .select_from(ReportDelivery)
        .where(
            ReportDelivery.created_at >= week,
            ReportDelivery.status == DeliveryStatus.FAILED,
        ),
    )
    return {
        "email_on_submit": bool(settings and settings.notify_on_submit),
        "cadences_emailed": list(settings.notify_cadences or []) if settings else [],
        "schedules": await _count(
            session, select(func.count()).select_from(ReportSchedule)
        ),
        "submitted_last_7_days": await _count(
            session,
            select(func.count())
            .select_from(Report)
            .where(Report.submitted_at >= week, Report.status == ReportStatus.SUBMITTED),
        ),
        "drafts_open": await _count(
            session,
            select(func.count())
            .select_from(Report)
            .where(Report.status == ReportStatus.DRAFT),
        ),
        "failed_deliveries_7_days": failed,
        "needs_attention": failed,
    }


async def _notifications_status(session: AsyncSession, user: User) -> dict[str, Any]:
    return {
        "mine_unread": await _count(
            session,
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user.id, Notification.read_at.is_(None)),
        ),
        "raised_total": await _count(
            session, select(func.count()).select_from(Notification)
        ),
        "needs_attention": 0,
    }


@router.get(
    "/console",
    response_model=ConsoleOut,
    summary="Every new admin surface, with what it currently says",
)
async def console(
    admin: SuperAdmin,
    session: Session,
    status_only: Annotated[
        bool, Query(description="Skip the endpoint lists and return just the figures.")
    ] = False,
) -> ConsoleOut:
    """The index for the administration screens, in one call.

    Each section carries its endpoints, a caution where there is a risk worth
    naming, and a small block of live figures. ``needs_attention`` on each one
    is the number a tile should badge — failed messages, failed deliveries, a
    mirror that has never synced — so a dashboard can be built without every
    caller re-deciding what counts as a problem.
    """
    figures: dict[str, dict[str, Any]] = {
        "intake": await _intake_status(session),
        "mirror": await _mirror_status(session),
        "standing": await _standing_status(session),
        "reports": await _reports_status(session),
        "notifications": await _notifications_status(session, admin),
    }

    sections = [
        SectionOut(
            key=s.key,
            name=s.name,
            audience=s.audience,
            description=s.description,
            caution=s.caution,
            endpoints=(
                []
                if status_only
                else [
                    EndpointOut(method=e.method, path=e.path, what=e.what, writes=e.writes)
                    for e in s.endpoints
                ]
            ),
            status=figures.get(s.key, {}),
        )
        for s in SECTIONS
    ]
    return ConsoleOut(
        sections=sections,
        needs_attention=sum(int(f.get("needs_attention", 0)) for f in figures.values()),
        generated_at=datetime.now(UTC),
    )


@router.get(
    "/permissions",
    response_model=list[PermissionRuleOut],
    summary="Who may do what, in the parts just built",
)
async def permissions(admin: SuperAdmin, session: Session) -> list[PermissionRuleOut]:
    """The rules behind these screens, in words, with who currently holds each role.

    The rules themselves live in code and a frontend cannot derive them from
    any endpoint — so they are restated here, because an admin screen that
    cannot explain *why* somebody is refused is a screen that generates support
    questions instead of answering them.
    """
    holders: dict[str, list[RoleHolderOut]] = {}
    rows = (
        await session.execute(
            select(Role.key, User.id, User.display_name, User.email)
            .join(UserRole, UserRole.role_id == Role.id)
            .join(User, User.id == UserRole.user_id)
            .where(Role.scope == RoleScope.GLOBAL)
            .order_by(Role.key, User.display_name)
        )
    ).all()
    for key, user_id, name, email in rows:
        holders.setdefault(key, []).append(
            RoleHolderOut(user_id=user_id, display_name=name, email=email)
        )

    return [
        PermissionRuleOut(
            area=rule.area,
            what=rule.what,
            who=list(rule.who),
            note=rule.note,
            # Only for global roles: the team-scoped ones are held per team and
            # listing every holder would be a different, much longer answer.
            holders=[h for key in rule.who for h in holders.get(key, [])],
        )
        for rule in PERMISSION_RULES
    ]
