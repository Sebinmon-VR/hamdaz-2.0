"""The dashboard cards each module contributes.

All of these read real data through the existing services. A new module adds its
widgets here (or registers them from its own package) and they become available
to every team that has that module.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.access import service as access_service
from app.dashboards.registry import Widget, WidgetContext, register
from app.directory.graph import GraphError
from app.models.role import Role
from app.models.team import TeamMembership
from app.proposals.analytics import scope_for_team
from app.proposals.sharepoint import SharePointError
from app.roles.catalogue import TEAM_LEAD
from app.teams import service as teams_service

# ── teams module ───────────────────────────────────────────────────────


async def _team_summary(ctx: WidgetContext) -> dict:
    members = await teams_service.list_members(ctx.session, ctx.team.id)
    leads = [u.display_name for u, rows in members if any(r.role.key == TEAM_LEAD for r in rows)]
    return {
        "name": ctx.team.name,
        "slug": ctx.team.slug,
        "description": ctx.team.description,
        "member_count": len(members),
        "leads": sorted(leads),
        "archived": ctx.team.is_archived,
        "created_at": ctx.team.created_at.isoformat(),
    }


async def _team_members(ctx: WidgetContext) -> dict:
    limit = int(ctx.options.get("limit", 10))
    members = await teams_service.list_members(ctx.session, ctx.team.id)
    return {
        "total": len(members),
        "showing": min(limit, len(members)),
        "members": [
            {
                "user_id": str(user.id),
                "display_name": user.display_name,
                "email": user.email,
                "role_keys": sorted(r.role.key for r in rows),
                "is_active": user.is_active,
            }
            for user, rows in members[:limit]
        ],
    }


async def _role_breakdown(ctx: WidgetContext) -> dict:
    """How many people hold each role in this team."""
    rows = await ctx.session.execute(
        select(Role.key, func.count(func.distinct(TeamMembership.user_id)))
        .join(TeamMembership, TeamMembership.role_id == Role.id)
        .where(TeamMembership.team_id == ctx.team.id)
        .group_by(Role.key)
        .order_by(Role.key)
    )
    counts = {key: count for key, count in rows.all()}
    return {"counts": counts, "total_roles_held": sum(counts.values())}


async def _recent_members(ctx: WidgetContext) -> dict:
    limit = int(ctx.options.get("limit", 5))
    members = await teams_service.list_members(ctx.session, ctx.team.id)
    # Sorted by when they joined rather than by name, which is what "recent" means.
    ordered = sorted(
        members, key=lambda pair: min(r.created_at for r in pair[1]), reverse=True
    )
    return {
        "members": [
            {
                "user_id": str(user.id),
                "display_name": user.display_name,
                "joined_at": min(r.created_at for r in rows).isoformat(),
                "role_keys": sorted(r.role.key for r in rows),
            }
            for user, rows in ordered[:limit]
        ]
    }


async def _my_standing(ctx: WidgetContext) -> dict:
    """What the viewer is in this team. Answers 'what can I do here'."""
    keys = await teams_service.team_role_keys(
        ctx.session, team_id=ctx.team.id, user_id=ctx.viewer.id
    )
    return {
        "display_name": ctx.viewer.display_name,
        "role_keys": sorted(keys),
        "is_member": bool(keys),
        "is_lead": TEAM_LEAD in keys,
    }


async def _team_modules(ctx: WidgetContext) -> dict:
    grants = await access_service.team_access(ctx.session, ctx.team.id)
    page_ids = await access_service.team_page_ids(ctx.session, ctx.team.id)
    return {
        "count": len(grants),
        "modules": [
            {
                "key": g.module_key,
                "name": g.module.name,
                "all_pages": g.all_pages,
                "pages": [
                    p.key
                    for p in sorted(g.module.pages, key=lambda p: p.sort_order)
                    if g.all_pages or p.id in page_ids
                ],
            }
            for g in grants
        ],
    }


# ── directory module ───────────────────────────────────────────────────


async def _directory_snapshot(ctx: WidgetContext) -> dict:
    """Headcount and departments, live from Entra."""
    try:
        people = await ctx.directory.list_users()
    except GraphError:
        # The card is unavailable, not the dashboard.
        return {"available": False, "reason": "The organisation directory is unreachable"}

    departments: dict[str, int] = {}
    for person in people:
        departments[person.department or "Unassigned"] = (
            departments.get(person.department or "Unassigned", 0) + 1
        )
    return {
        "available": True,
        "headcount": len(people),
        "departments": dict(sorted(departments.items(), key=lambda kv: -kv[1])),
    }


# ── proposals module ───────────────────────────────────────────────────


async def _my_proposal_tasks(ctx: WidgetContext) -> dict:
    """The viewer's own proposal tasks — never anybody else's.

    This card sits on a *team* dashboard but is still personal: two people
    looking at the same team see different rows. That is the rule the proposals
    module is built on, and a dashboard is not an exception to it.
    """
    if ctx.sharepoint is None:
        return {"available": False, "reason": "SharePoint is not configured"}

    limit = int(ctx.options.get("limit", 8))
    try:
        lookup_id = await ctx.sharepoint.lookup_id_for(ctx.viewer.email)
        if lookup_id is None:
            return {
                "available": True,
                "in_sharepoint": False,
                "open_count": 0,
                "tasks": [],
            }
        tasks = await ctx.sharepoint.tasks_assigned_to(lookup_id, limit=200)
    except SharePointError:
        return {"available": False, "reason": "The Proposals list is unreachable"}

    open_tasks = [t for t in tasks if t.is_open]
    soonest = sorted(open_tasks, key=lambda t: (t.due_date is None, t.due_date or ""))
    return {
        "available": True,
        "in_sharepoint": True,
        "total": len(tasks),
        "open_count": len(open_tasks),
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "due_date": t.due_date,
                "end_user": t.end_user,
                "web_url": t.web_url,
            }
            for t in soonest[:limit]
        ],
    }


async def _proposal_workload(ctx: WidgetContext) -> dict:
    """Everyone's proposal counts — one card, one fetch.

    Deliberately not several widgets. Every number here comes from the same
    sweep of the list, so splitting it up would mean repeating that sweep (or
    sharing a cache, at which point the split buys nothing). The frontend can
    render this single payload as as many tiles as it likes.

    Admin-only: a card that shows other people's workloads has no business on a
    non-admin's dashboard, whatever module their team holds.
    """
    if ctx.sharepoint is None or ctx.workload_cache is None:
        return {"available": False, "reason": "SharePoint is not configured"}

    from app.roles.catalogue import ADMIN_ROLES
    from app.roles.service import global_role_keys

    roles = await global_role_keys(ctx.session, ctx.viewer.id)
    if ADMIN_ROLES.isdisjoint(roles):
        return {"available": False, "reason": "Admins only"}

    try:
        # Scoped to this team's members. The list carries plenty of people who
        # are not on the team, and a team dashboard reporting their workload
        # would be answering a question nobody asked.
        scope = await scope_for_team(ctx.session, ctx.team, ctx.sharepoint)
        only = scope.pop("lookup_ids")
        summary = await ctx.workload_cache.get(ctx.sharepoint, only=only)
    except SharePointError:
        return {"available": False, "reason": "The Proposals list is unreachable"}

    limit = int(ctx.options.get("limit", 10))
    return {
        "available": True,
        "scope": scope,
        "organisation": summary["organisation"],
        "soon_days": summary["soon_days"],
        "person_count": summary["person_count"],
        # What the scope left out, so an empty card explains itself.
        "excluded": summary["excluded"],
        "cached": summary.get("cached", False),
        # Already sorted busiest-first by the aggregator.
        "people": summary["people"][:limit],
    }


# ── leave module ───────────────────────────────────────────────────────


async def _my_leave(ctx: WidgetContext) -> dict:
    """The viewer's own leave. Personal even on a team dashboard."""
    from app.leave import service as leave_service

    summary = await leave_service.summary(ctx.session, ctx.viewer.id)
    settings = await leave_service.get_settings(ctx.session)
    return {
        **summary,
        "max_concurrent": settings.max_concurrent,
        "auto_decide": settings.auto_decide,
    }


async def _who_is_off(ctx: WidgetContext) -> dict:
    """Approved leave over the next fortnight, so clashes are visible early."""
    from datetime import date, timedelta

    from app.leave import service as leave_service

    days = int(ctx.options.get("days", 14))
    start = date.today()
    calendar = await leave_service.calendar(
        ctx.session, start=start, end=start + timedelta(days=days - 1)
    )
    settings = await leave_service.get_settings(ctx.session)
    return {
        "start": start.isoformat(),
        "days": days,
        "limit": settings.max_concurrent,
        # Only days with somebody off appear, so the card is short by default.
        "calendar": calendar,
        # Days already at the limit — the ones worth planning around.
        "full_days": sorted(
            d for d, who in calendar.items() if len(who) >= settings.max_concurrent
        ),
    }


async def _leave_queue(ctx: WidgetContext) -> dict:
    """What HR still has to decide. Visible only to the HR team."""
    from app.leave import service as leave_service
    from app.models.leave import LeaveStatus

    if not await leave_service.is_hr(ctx.session, ctx.viewer.id):
        return {"available": False, "reason": "HR only"}

    limit = int(ctx.options.get("limit", 10))
    pending = await leave_service.all_requests(
        ctx.session, status=LeaveStatus.PENDING, limit=200
    )
    rejected = await leave_service.all_requests(
        ctx.session, status=LeaveStatus.REJECTED, upcoming_only=True, limit=200
    )
    return {
        "available": True,
        "pending_count": len(pending),
        # Auto-rejected requests still ahead of us: the ones somebody may ask
        # HR to override.
        "auto_rejected_upcoming": len(
            [r for r in rejected if r.decided_by == "system"]
        ),
        "requests": [
            {
                "id": str(r.id),
                "name": r.user.display_name,
                "leave_type": r.leave_type,
                "start_date": r.start_date.isoformat(),
                "end_date": r.end_date.isoformat(),
                "days": r.days,
                "reason": r.reason,
            }
            for r in pending[:limit]
        ],
    }


# ── registration ───────────────────────────────────────────────────────

register(Widget(
    key="team_summary",
    title="Team overview",
    description="Name, description, member count and who leads the team.",
    module="teams",
    load=_team_summary,
    size="medium",
    default=True,
))
register(Widget(
    key="my_standing",
    title="My standing here",
    description="The roles the viewer holds in this team.",
    module="teams",
    load=_my_standing,
    size="small",
    default=True,
))
register(Widget(
    key="team_members",
    title="Members",
    description="Who is in the team and what they hold. options: limit.",
    module="teams",
    load=_team_members,
    size="large",
    default=True,
))
register(Widget(
    key="role_breakdown",
    title="Roles at a glance",
    description="How many people hold each role in this team.",
    module="teams",
    load=_role_breakdown,
    size="small",
))
register(Widget(
    key="recent_members",
    title="Recently added",
    description="The newest members of the team. options: limit.",
    module="teams",
    load=_recent_members,
    size="medium",
))
register(Widget(
    key="team_modules",
    title="What this team can reach",
    description="The modules and pages granted to this team.",
    module="teams",
    load=_team_modules,
    size="medium",
    default=True,
))
register(Widget(
    key="directory_snapshot",
    title="Organisation snapshot",
    description="Headcount and department split, live from Entra.",
    module="directory",
    load=_directory_snapshot,
    size="medium",
    remote=True,
))
register(Widget(
    key="my_proposal_tasks",
    title="My proposal tasks",
    description="Open proposal tasks assigned to the viewer. options: limit.",
    module="proposals",
    load=_my_proposal_tasks,
    size="large",
    default=True,
    remote=True,
))
register(Widget(
    key="proposal_workload",
    title="Proposal workload by person",
    description=(
        "Totals, completed, open, overdue and due-soon for everyone. "
        "Admins only. options: limit."
    ),
    module="proposals",
    load=_proposal_workload,
    size="full",
    remote=True,
))
register(Widget(
    key="my_leave",
    title="My leave",
    description="The viewer's own requests, and what is coming up.",
    module="leave",
    load=_my_leave,
    size="medium",
    default=True,
))
register(Widget(
    key="who_is_off",
    title="Who is off",
    description="Approved leave over the next fortnight. options: days.",
    module="leave",
    load=_who_is_off,
    size="large",
    default=True,
))
register(Widget(
    key="leave_queue",
    title="Leave to decide",
    description="Pending requests waiting on HR. HR only. options: limit.",
    module="leave",
    load=_leave_queue,
    size="large",
    default=True,
))
