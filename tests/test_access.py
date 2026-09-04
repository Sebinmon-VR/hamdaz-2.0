"""Module visibility: what a team is granted, and what a person ends up seeing.

The model is allow-list, so the tests that matter most are the ones proving
things are *not* visible — a permission system that grants correctly but fails
to withhold is the one that leaks.
"""

from __future__ import annotations

import pytest

from app.access import service
from app.access.catalogue import BY_KEY, GRANTABLE, MODULES
from app.access.service import AccessConflictError, AccessNotFoundError
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.roles import service as roles
from app.teams import service as teams


async def _user(db, email: str = "person@hamdaz.com"):
    return await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )


@pytest.fixture
async def seeded(db):
    await roles.seed_system_roles(db)
    await service.seed_modules(db)
    await db.commit()


@pytest.fixture
async def team(db, seeded):
    t = await teams.create_team(db, name="Site Operations")
    await db.commit()
    return t


# ── the catalogue ──────────────────────────────────────────────────────


def test_admin_only_modules_are_not_grantable() -> None:
    """Reaching them depends on a global role, so a team grant would be a lie."""
    grantable = {m.key for m in GRANTABLE}
    assert "roles" not in grantable
    assert "user_admin" not in grantable
    assert "directory" in grantable


def test_page_keys_are_unique_within_a_module() -> None:
    for module in MODULES:
        keys = [p.key for p in module.pages]
        assert len(keys) == len(set(keys)), f"{module.key} has duplicate page keys"


async def test_seeding_mirrors_the_code_catalogue(db, seeded) -> None:
    rows = await service.list_modules(db)
    assert [m.key for m in rows] == [m.key for m in MODULES]
    for row in rows:
        assert {p.key for p in row.pages} == {p.key for p in BY_KEY[row.key].pages}


async def test_seeding_twice_changes_nothing(db, seeded) -> None:
    await service.seed_modules(db)
    await db.commit()
    rows = await service.list_modules(db)
    assert len(rows) == len(MODULES)
    assert len(rows[1].pages) == len(BY_KEY[rows[1].key].pages)


async def test_unknown_module_raises(db, seeded) -> None:
    with pytest.raises(AccessNotFoundError):
        await service.get_module(db, "payroll")


# ── granting ───────────────────────────────────────────────────────────


async def test_a_team_starts_with_nothing(db, team) -> None:
    """Allow-list: a new module must not silently appear for everyone."""
    assert await service.team_access(db, team.id) == []


async def test_granting_a_whole_module(db, team) -> None:
    grant = await service.grant_module(db, team=team, module_key="directory")
    assert grant.all_pages is True
    assert await service.team_page_ids(db, team.id) == set()


async def test_granting_specific_pages(db, team) -> None:
    grant = await service.grant_module(db, team=team, module_key="teams", page_keys=["list"])
    assert grant.all_pages is False
    assert len(await service.team_page_ids(db, team.id)) == 1


async def test_regranting_replaces_the_page_set(db, team) -> None:
    """Repeating a call must not accumulate page rows."""
    await service.grant_module(db, team=team, module_key="teams", page_keys=["list", "detail"])
    await db.commit()
    await service.grant_module(db, team=team, module_key="teams", page_keys=["members"])
    await db.commit()

    assert len(await service.team_page_ids(db, team.id)) == 1


async def test_widening_a_grant_to_the_whole_module(db, team) -> None:
    await service.grant_module(db, team=team, module_key="teams", page_keys=["list"])
    await db.commit()
    grant = await service.grant_module(db, team=team, module_key="teams")
    await db.commit()

    assert grant.all_pages is True
    assert await service.team_page_ids(db, team.id) == set()


async def test_admin_only_modules_are_refused(db, team) -> None:
    with pytest.raises(AccessConflictError, match="global admin role"):
        await service.grant_module(db, team=team, module_key="roles")


async def test_an_unknown_page_is_refused(db, team) -> None:
    with pytest.raises(AccessNotFoundError, match="no page"):
        await service.grant_module(db, team=team, module_key="teams", page_keys=["nope"])


async def test_an_empty_page_list_is_refused(db, team) -> None:
    """Ambiguous: omit pages for the whole module, or name at least one."""
    with pytest.raises(AccessConflictError, match="at least one page"):
        await service.grant_module(db, team=team, module_key="teams", page_keys=[])


async def test_revoking(db, team) -> None:
    await service.grant_module(db, team=team, module_key="teams", page_keys=["list"])
    await db.commit()
    await service.revoke_module(db, team=team, module_key="teams")
    await db.commit()

    assert await service.team_access(db, team.id) == []
    assert await service.team_page_ids(db, team.id) == set()


async def test_revoking_what_was_never_granted_raises(db, team) -> None:
    with pytest.raises(AccessNotFoundError):
        await service.revoke_module(db, team=team, module_key="directory")


async def test_set_access_replaces_everything(db, team) -> None:
    await service.grant_module(db, team=team, module_key="directory")
    await db.commit()

    await service.set_team_access(
        db, team=team, modules={"teams": ["list"], "dashboard": None}
    )
    await db.commit()

    keys = {g.module_key for g in await service.team_access(db, team.id)}
    assert keys == {"teams", "dashboard"}


async def test_set_access_to_nothing_clears_it(db, team) -> None:
    await service.grant_module(db, team=team, module_key="directory")
    await db.commit()
    await service.set_team_access(db, team=team, modules={})
    await db.commit()
    assert await service.team_access(db, team.id) == []


async def test_deleting_a_team_removes_its_grants(db, team) -> None:
    await service.grant_module(db, team=team, module_key="directory")
    await db.commit()
    await teams.archive_team(db, team.slug)
    await teams.delete_team(db, team.slug)
    await db.commit()

    assert await service.team_access(db, team.id) == []


# ── what a person sees ─────────────────────────────────────────────────


async def _effective(db, user, *global_roles):
    return await service.effective_access(db, user_id=user.id, global_roles=global_roles)


async def test_someone_in_no_team_sees_nothing(db, seeded) -> None:
    user = await _user(db)
    await db.commit()
    assert (await _effective(db, user))["modules"] == []


async def test_a_team_member_sees_the_team_s_modules(db, team) -> None:
    user = await _user(db)
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await service.grant_module(db, team=team, module_key="directory")
    await db.commit()

    access = await _effective(db, user)
    assert [m["key"] for m in access["modules"]] == ["directory"]
    assert access["via_teams"] == [team.slug]


async def test_a_page_limited_grant_hides_the_other_pages(db, team) -> None:
    user = await _user(db)
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await service.grant_module(db, team=team, module_key="teams", page_keys=["list"])
    await db.commit()

    access = await _effective(db, user)
    assert [p["key"] for p in access["modules"][0]["pages"]] == ["list"]


async def test_access_is_the_union_of_every_team(db, seeded) -> None:
    alpha = await teams.create_team(db, name="Alpha")
    beta = await teams.create_team(db, name="Beta")
    user = await _user(db)
    for t in (alpha, beta):
        await teams.set_member_roles(db, team=t, user=user, role_keys=["member"])
    await service.grant_module(db, team=alpha, module_key="teams", page_keys=["list"])
    await service.grant_module(db, team=beta, module_key="teams", page_keys=["members"])
    await service.grant_module(db, team=beta, module_key="dashboard")
    await db.commit()

    access = await _effective(db, user)
    by_key = {m["key"]: [p["key"] for p in m["pages"]] for m in access["modules"]}
    assert sorted(by_key["teams"]) == ["list", "members"]
    # The dashboard module carries a personal page and a team-scoped one.
    assert sorted(by_key["dashboard"]) == ["overview", "team"]


async def test_the_wider_grant_wins_in_a_union(db, seeded) -> None:
    """One team with the whole module beats another with a single page."""
    alpha = await teams.create_team(db, name="Alpha")
    beta = await teams.create_team(db, name="Beta")
    user = await _user(db)
    for t in (alpha, beta):
        await teams.set_member_roles(db, team=t, user=user, role_keys=["member"])
    await service.grant_module(db, team=alpha, module_key="teams", page_keys=["list"])
    await service.grant_module(db, team=beta, module_key="teams")
    await db.commit()

    access = await _effective(db, user)
    pages = [p["key"] for p in access["modules"][0]["pages"]]
    assert sorted(pages) == ["detail", "list", "members"]


async def test_leaving_a_team_removes_its_access(db, team) -> None:
    user = await _user(db)
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await service.grant_module(db, team=team, module_key="directory")
    await db.commit()

    await teams.remove_member(db, team=team, user_id=user.id)
    await db.commit()

    assert (await _effective(db, user))["modules"] == []


async def test_a_super_admin_sees_everything_without_a_team(db, seeded) -> None:
    """Otherwise a super admin in no team could not reach the screen that grants access."""
    user = await _user(db)
    await db.commit()

    access = await _effective(db, user, "super_admin")
    assert access["source"] == "super_admin"
    assert {m["key"] for m in access["modules"]} == {m.key for m in MODULES}


async def test_a_manager_does_not_get_blanket_visibility(db, seeded) -> None:
    """Being an admin elsewhere does not bypass the allow-list."""
    user = await _user(db)
    await db.commit()
    access = await _effective(db, user, "manager")
    assert access["source"] == "teams"
    assert access["modules"] == []


# ── the yes/no helper future modules will guard with ───────────────────


async def test_can_reach_module_and_page(db, team) -> None:
    user = await _user(db)
    await teams.set_member_roles(db, team=team, user=user, role_keys=["member"])
    await service.grant_module(db, team=team, module_key="teams", page_keys=["list"])
    await db.commit()

    async def reach(module, page=None):
        return await service.can_reach(
            db, user_id=user.id, global_roles=[], module_key=module, page_key=page
        )

    assert await reach("teams") is True
    assert await reach("teams", "list") is True
    assert await reach("teams", "members") is False
    assert await reach("directory") is False


async def test_can_reach_is_true_for_a_super_admin(db, seeded) -> None:
    user = await _user(db)
    await db.commit()
    assert (
        await service.can_reach(
            db,
            user_id=user.id,
            global_roles=["super_admin"],
            module_key="user_admin",
            page_key="access",
        )
        is True
    )
