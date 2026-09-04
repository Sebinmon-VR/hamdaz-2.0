"""What each module contributes to a user profile.

One entry per module. A new module adds its ``Section`` here (or registers one
from its own package) and both ``GET /users/{id}`` and the reset pick it up with
no other change.

Note which sections have a purger and which do not. ``directory`` deliberately
has none: that record lives in Entra, and "reset this user in the ERP" must never
reach out and alter the company directory.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select

from app.directory.graph import GraphError
from app.models.role import UserRole
from app.models.team import TeamMembership
from app.profiles.registry import LoadContext, PurgeContext, Section, register
from app.roles import service as roles_service
from app.teams import service as teams_service

# ── identity: the local user row ───────────────────────────────────────


async def _load_identity(ctx: LoadContext) -> dict:
    user = ctx.user
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "entra_object_id": user.entra_object_id,
        "is_active": user.is_active,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
        #: Provisioned by an admin and never signed in — worth surfacing, since
        #: it explains an account that exists but has no activity.
        "has_signed_in": user.last_login_at is not None,
    }


# ── directory: the live Entra record ───────────────────────────────────


async def _load_directory(ctx: LoadContext) -> dict | None:
    try:
        person = await ctx.directory.get_user(ctx.user.entra_object_id)
    except GraphError:
        # They may have left the company, or Graph may simply be unreachable.
        # Neither should fail the whole profile.
        return None
    return {
        "object_id": person.object_id,
        "display_name": person.display_name,
        "email": person.email,
        "user_principal_name": person.user_principal_name,
        "job_title": person.job_title,
        "department": person.department,
        "office_location": person.office_location,
        "mobile_phone": person.mobile_phone,
        "account_enabled": person.account_enabled,
        "is_guest": person.is_guest,
    }


# ── roles: organisation-wide grants ────────────────────────────────────


async def _load_roles(ctx: LoadContext) -> dict:
    rows = await roles_service.list_user_roles(ctx.session, ctx.user.id)
    keys = sorted(row.role.key for row in rows)
    from app.roles.catalogue import ADMIN_ROLES, SUPER_ADMIN

    return {
        "keys": keys,
        "is_admin": not ADMIN_ROLES.isdisjoint(keys),
        "is_super_admin": SUPER_ADMIN in keys,
        "grants": [
            {
                "key": row.role.key,
                "name": row.role.name,
                "granted_at": row.created_at.isoformat(),
                "granted_by_id": str(row.granted_by_id) if row.granted_by_id else None,
            }
            for row in rows
        ],
    }


async def _purge_roles(ctx: PurgeContext) -> int:
    result = await ctx.session.execute(
        delete(UserRole).where(UserRole.user_id == ctx.user.id)
    )
    return result.rowcount or 0


# ── teams: membership and the roles held inside each ───────────────────


async def _load_teams(ctx: LoadContext) -> dict:
    pairs = await teams_service.teams_for_user(ctx.session, ctx.user.id, include_archived=True)
    return {
        "count": len(pairs),
        "memberships": [
            {
                "team_id": str(team.id),
                "slug": team.slug,
                "name": team.name,
                "archived": team.is_archived,
                "role_keys": keys,
                "is_lead": "team_lead" in keys,
            }
            for team, keys in pairs
        ],
    }


async def _purge_teams(ctx: PurgeContext) -> int:
    result = await ctx.session.execute(
        delete(TeamMembership).where(TeamMembership.user_id == ctx.user.id)
    )
    return result.rowcount or 0


# ── audit trail this user left on others ───────────────────────────────


async def _load_granted_by_them(ctx: LoadContext) -> dict:
    """Roles and memberships this person handed out.

    Kept separate from their own access because it matters when removing them:
    these references are what would break if the user row were deleted, and the
    schema deliberately nulls them rather than cascading.
    """
    # Two scalar subqueries in one statement: at ~310 ms per round trip, two
    # sequential COUNTs would cost twice as much for the same answer.
    row = (
        await ctx.session.execute(
            select(
                select(func.count())
                .select_from(UserRole)
                .where(UserRole.granted_by_id == ctx.user.id)
                .scalar_subquery(),
                select(func.count())
                .select_from(TeamMembership)
                .where(TeamMembership.added_by_id == ctx.user.id)
                .scalar_subquery(),
            )
        )
    ).one()
    return {"roles_granted": row[0] or 0, "team_members_added": row[1] or 0}


# ── registration order is response order ───────────────────────────────

register(Section(key="identity", label="Identity", load=_load_identity))
register(Section(key="directory", label="Entra directory", load=_load_directory, remote=True))
register(Section(key="roles", label="Global roles", load=_load_roles, purge=_purge_roles))
register(Section(key="teams", label="Team membership", load=_load_teams, purge=_purge_teams))
register(Section(key="activity", label="Actions taken", load=_load_granted_by_them))
