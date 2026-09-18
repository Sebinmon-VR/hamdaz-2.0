"""Granting modules to teams, and working out what a person can actually see.

Two directions:

* **administration** — a super admin sets which modules each team gets
* **resolution** — a signed-in user asks "what can I see", answered as the union
  of every team they belong to

The union matters: someone in two teams gets everything either team has. Taking a
module off one team does not remove it from someone who also belongs to another
team that still has it, which is the correct behaviour and the one people find
surprising, so it is stated here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.access.catalogue import ACCESS_ADMINS, MODULES
from app.roles.catalogue import ADMIN_ROLES
from app.models.access import Module, ModulePage, TeamModuleAccess, TeamPageAccess
from app.models.role import Role
from app.models.team import Team, TeamMembership


class AccessError(Exception):
    """An access operation was refused. Safe to show a user."""


class AccessNotFoundError(AccessError):
    pass


class AccessConflictError(AccessError):
    pass


def may_administer(role_keys: Iterable[str]) -> bool:
    return not ACCESS_ADMINS.isdisjoint(set(role_keys))


# ── the catalogue ──────────────────────────────────────────────────────


async def seed_modules(session: AsyncSession) -> list[Module]:
    """Bring the modules table in line with the code catalogue.

    Safe to run on every deploy. Pages that disappear from the code are removed;
    a grant referencing them goes with them, which is right — the page no longer
    exists to be reached.
    """
    existing = {m.key: m for m in (await session.scalars(select(Module))).all()}
    # Pages are queried directly rather than through Module.pages: a Module just
    # added to the session has no loaded collection, and touching the
    # relationship would trigger a lazy load in async context (MissingGreenlet).
    all_pages = (await session.scalars(select(ModulePage))).all()
    pages_by_module: dict[str, dict[str, ModulePage]] = {}
    for page in all_pages:
        pages_by_module.setdefault(page.module_key, {})[page.key] = page

    seeded: list[Module] = []

    for order, spec in enumerate(MODULES):
        module = existing.get(spec.key)
        if module is None:
            module = Module(key=spec.key, name=spec.name, description=spec.description)
            session.add(module)
        module.name = spec.name
        module.description = spec.description
        module.admin_only = spec.admin_only
        module.sort_order = order
        await session.flush()

        current = pages_by_module.get(spec.key, {})
        wanted = {p.key for p in spec.pages}

        for page_order, page_spec in enumerate(spec.pages):
            page = current.get(page_spec.key)
            if page is None:
                page = ModulePage(module_key=module.key, key=page_spec.key)
                session.add(page)
            page.name = page_spec.name
            page.path = page_spec.path
            page.team_scoped = page_spec.team_scoped
            page.sort_order = page_order

        # A page removed from the code no longer exists to be reached, so grants
        # referencing it go with it.
        for key, page in current.items():
            if key not in wanted:
                await session.delete(page)

        seeded.append(module)

    await session.flush()
    return seeded


async def list_modules(session: AsyncSession) -> list[Module]:
    return list(
        (
            await session.scalars(
                select(Module).options(selectinload(Module.pages)).order_by(Module.sort_order)
            )
        ).all()
    )


async def get_module(session: AsyncSession, key: str) -> Module:
    module = await session.scalar(
        select(Module).where(Module.key == key).options(selectinload(Module.pages))
    )
    if module is None:
        raise AccessNotFoundError(f"No module named {key!r}")
    return module


# ── granting ───────────────────────────────────────────────────────────


async def team_access(session: AsyncSession, team_id: uuid.UUID) -> list[TeamModuleAccess]:
    return list(
        (
            await session.scalars(
                select(TeamModuleAccess)
                .where(TeamModuleAccess.team_id == team_id)
                .options(selectinload(TeamModuleAccess.module).selectinload(Module.pages))
            )
        ).all()
    )


async def team_page_ids(session: AsyncSession, team_id: uuid.UUID) -> set[uuid.UUID]:
    ids = await session.scalars(
        select(TeamPageAccess.page_id).where(TeamPageAccess.team_id == team_id)
    )
    return set(ids.all())


async def grant_module(
    session: AsyncSession,
    *,
    team: Team,
    module_key: str,
    page_keys: list[str] | None = None,
    granted_by_id: uuid.UUID | None = None,
) -> TeamModuleAccess:
    """Give a team a module, optionally narrowed to specific pages.

    ``page_keys=None`` means the whole module, including pages added to it later.
    An explicit list pins the grant to exactly those pages.
    """
    module = await get_module(session, module_key)
    if module.admin_only:
        raise AccessConflictError(
            f"{module.key!r} is reached through a global admin role, not a team grant"
        )

    grant = await session.scalar(
        select(TeamModuleAccess).where(
            TeamModuleAccess.team_id == team.id, TeamModuleAccess.module_key == module.key
        )
    )
    if grant is None:
        grant = TeamModuleAccess(
            team_id=team.id, module_key=module.key, granted_by_id=granted_by_id
        )
        session.add(grant)

    # Page rows are replaced wholesale, so repeating a call cannot accumulate.
    page_ids = [p.id for p in module.pages]
    if page_ids:
        await session.execute(
            delete(TeamPageAccess).where(
                TeamPageAccess.team_id == team.id, TeamPageAccess.page_id.in_(page_ids)
            )
        )

    if page_keys is None:
        grant.all_pages = True
    else:
        by_key = {p.key: p for p in module.pages}
        unknown = [k for k in page_keys if k not in by_key]
        if unknown:
            raise AccessNotFoundError(
                f"{module.key!r} has no page(s): {', '.join(sorted(unknown))}"
            )
        if not page_keys:
            raise AccessConflictError(
                "A page-limited grant needs at least one page; omit pages for the whole module"
            )
        grant.all_pages = False
        for key in dict.fromkeys(page_keys):
            session.add(TeamPageAccess(team_id=team.id, page_id=by_key[key].id))

    await session.flush()
    return grant


async def revoke_module(session: AsyncSession, *, team: Team, module_key: str) -> None:
    grant = await session.scalar(
        select(TeamModuleAccess).where(
            TeamModuleAccess.team_id == team.id, TeamModuleAccess.module_key == module_key
        )
    )
    if grant is None:
        raise AccessNotFoundError(f"That team does not have {module_key!r}")

    module = await get_module(session, module_key)
    page_ids = [p.id for p in module.pages]
    if page_ids:
        await session.execute(
            delete(TeamPageAccess).where(
                TeamPageAccess.team_id == team.id, TeamPageAccess.page_id.in_(page_ids)
            )
        )
    await session.delete(grant)
    await session.flush()


async def set_team_access(
    session: AsyncSession,
    *,
    team: Team,
    modules: dict[str, list[str] | None],
    granted_by_id: uuid.UUID | None = None,
) -> list[TeamModuleAccess]:
    """Make the team's access exactly ``modules``.

    One call to describe the whole picture, so an admin screen can save a form
    without working out which individual grants to add and remove.
    """
    await session.execute(
        delete(TeamPageAccess).where(TeamPageAccess.team_id == team.id)
    )
    await session.execute(
        delete(TeamModuleAccess).where(TeamModuleAccess.team_id == team.id)
    )
    await session.flush()

    for key, pages in modules.items():
        await grant_module(
            session, team=team, module_key=key, page_keys=pages, granted_by_id=granted_by_id
        )
    return await team_access(session, team.id)


# ── resolving what a person can see ────────────────────────────────────


async def effective_access(
    session: AsyncSession, *, user_id: uuid.UUID, global_roles: Iterable[str]
) -> dict:
    """Every module and page this user can reach.

    A super admin sees everything unconditionally. Without that, a super admin
    who belongs to no team would see nothing — including the screen used to
    grant access, which would be an unrecoverable state.
    """
    modules = await list_modules(session)
    roles = set(global_roles)

    if may_administer(roles):
        return {
            "source": "super_admin",
            "modules": [
                {
                    "key": m.key,
                    "name": m.name,
                    "admin_only": m.admin_only,
                    "pages": [
                        {
                            "key": p.key,
                            "name": p.name,
                            "path": p.path,
                            "team_scoped": p.team_scoped,
                        }
                        for p in sorted(m.pages, key=lambda p: p.sort_order)
                    ],
                }
                for m in modules
            ],
            "via_teams": [],
        }

    team_ids = list(
        (
            await session.scalars(
                select(TeamMembership.team_id).where(TeamMembership.user_id == user_id).distinct()
            )
        ).all()
    )
    if not team_ids:
        return {"source": "teams", "modules": [], "via_teams": []}

    grants = list(
        (
            await session.scalars(
                select(TeamModuleAccess)
                .where(TeamModuleAccess.team_id.in_(team_ids))
                .options(selectinload(TeamModuleAccess.module).selectinload(Module.pages))
            )
        ).all()
    )

    # A few modules ask for more than membership of a granted team — what they
    # show is sensitive inside a team, not only between teams. For those, the
    # grant says the team may use the module and the role says who in that team
    # may see it. See ``ModuleSpec.requires_team_roles``.
    restricted = {
        spec.key: spec.requires_team_roles for spec in MODULES if spec.requires_team_roles
    }
    if restricted:
        # Held per team, because holding `approver` in one team should not open
        # a restricted module that a different team granted. The pair is the
        # unit of authority here, not the role on its own.
        held: set[tuple[uuid.UUID, str]] = {
            (team_id, key)
            for team_id, key in (
                await session.execute(
                    select(TeamMembership.team_id, Role.key)
                    .join(Role, Role.id == TeamMembership.role_id)
                    .where(TeamMembership.user_id == user_id)
                )
            ).all()
        }
        # A global manager, CEO or super admin answers for the business rather
        # than for a team, and is not filtered by a team role they never hold.
        privileged = bool(roles & ADMIN_ROLES)
        if not privileged:
            grants = [
                grant
                for grant in grants
                if grant.module_key not in restricted
                or any(
                    (grant.team_id, key) in held for key in restricted[grant.module_key]
                )
            ]
    page_rows = list(
        (
            await session.scalars(
                select(TeamPageAccess)
                .where(TeamPageAccess.team_id.in_(team_ids))
                .options(selectinload(TeamPageAccess.page))
            )
        ).all()
    )
    granted_page_ids = {row.page_id for row in page_rows}

    # Union across teams: two teams granting the same module, one whole and one
    # page-limited, gives the whole module. The wider grant wins.
    by_module: dict[str, set[str] | None] = {}
    for grant in grants:
        module = grant.module
        if grant.all_pages:
            by_module[module.key] = None
            continue
        if by_module.get(module.key, "missing") is None:
            continue
        chosen = {
            p.key for p in module.pages if p.id in granted_page_ids
        }
        by_module.setdefault(module.key, set())
        current = by_module[module.key]
        if current is not None:
            current |= chosen

    catalogue = {m.key: m for m in modules}
    out = []
    for key, pages in by_module.items():
        module = catalogue.get(key)
        if module is None:
            continue
        ordered = sorted(module.pages, key=lambda p: p.sort_order)
        visible = ordered if pages is None else [p for p in ordered if p.key in pages]
        out.append(
            {
                "key": module.key,
                "name": module.name,
                "admin_only": module.admin_only,
                "pages": [
                    {
                        "key": p.key,
                        "name": p.name,
                        "path": p.path,
                        "team_scoped": p.team_scoped,
                    }
                    for p in visible
                ],
            }
        )
    out.sort(key=lambda m: catalogue[m["key"]].sort_order)

    teams = list(
        (await session.scalars(select(Team).where(Team.id.in_(team_ids)))).all()
    )
    return {
        "source": "teams",
        "modules": out,
        "via_teams": sorted(t.slug for t in teams),
    }


async def can_reach(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    global_roles: Iterable[str],
    module_key: str,
    page_key: str | None = None,
) -> bool:
    """A single yes/no, for guarding a future module's endpoints."""
    access = await effective_access(session, user_id=user_id, global_roles=global_roles)
    for module in access["modules"]:
        if module["key"] != module_key:
            continue
        if page_key is None:
            return True
        return any(p["key"] == page_key for p in module["pages"])
    return False
