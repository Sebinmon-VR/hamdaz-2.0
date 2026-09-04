"""The role service: the catalogue and global grants.

The tests that matter most are the refusals. A permission system that grants
correctly but refuses incorrectly is the one that gets exploited.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.models.role import Role, RoleScope
from app.roles import service
from app.roles.service import RoleConflictError, RoleError, RoleNotFoundError


async def _user(db, email: str = "person@hamdaz.com", oid: str | None = None):
    return await upsert_user(
        db, EntraIdentity(object_id=oid or email, email=email, display_name=email.split("@")[0])
    )


# ── seeding ────────────────────────────────────────────────────────────


async def test_seeds_the_six_system_roles(db) -> None:
    roles = await service.seed_system_roles(db)
    assert {r.key for r in roles} == {
        "super_admin", "ceo", "manager", "team_lead", "member", "approver",
    }
    assert all(r.is_system for r in roles)


async def test_seed_splits_global_from_team_scope(db) -> None:
    await service.seed_system_roles(db)
    by_key = {r.key: r for r in await service.list_roles(db)}

    assert {k for k, r in by_key.items() if r.scope == RoleScope.GLOBAL} == {
        "super_admin", "ceo", "manager",
    }
    assert {k for k, r in by_key.items() if r.scope == RoleScope.TEAM} == {
        "team_lead", "member", "approver",
    }


async def test_seeding_twice_creates_nothing_new(db) -> None:
    await service.seed_system_roles(db)
    await db.commit()
    await service.seed_system_roles(db)
    await db.commit()
    assert len(await service.list_roles(db)) == 6


async def test_seed_repairs_a_corrupted_scope(db) -> None:
    """Scope is structural — code branches on it — so the seeder resets it."""
    await service.seed_system_roles(db)
    role = await service.get_role(db, "ceo")
    role.scope = RoleScope.TEAM
    await db.flush()

    await service.seed_system_roles(db)
    assert (await service.get_role(db, "ceo")).scope == RoleScope.GLOBAL


async def test_seed_leaves_a_renamed_role_alone(db) -> None:
    """An admin may deliberately reword a role; the seeder must not undo that."""
    await service.seed_system_roles(db)
    await service.update_role(db, "manager", name="Head of Department")

    await service.seed_system_roles(db)
    assert (await service.get_role(db, "manager")).name == "Head of Department"


# ── the catalogue ──────────────────────────────────────────────────────


async def test_creates_a_custom_role(db) -> None:
    role = await service.create_role(
        db, key="auditor", name="Auditor", scope=RoleScope.GLOBAL, description="Reads everything"
    )
    assert role.key == "auditor"
    assert role.is_system is False


async def test_role_key_is_normalised(db) -> None:
    role = await service.create_role(db, key="  Auditor  ", name="A", scope=RoleScope.GLOBAL)
    assert role.key == "auditor"


async def test_duplicate_role_key_is_refused(db) -> None:
    await service.seed_system_roles(db)
    with pytest.raises(RoleConflictError, match="already exists"):
        await service.create_role(db, key="manager", name="Another", scope=RoleScope.GLOBAL)


async def test_blank_role_key_is_refused(db) -> None:
    with pytest.raises(RoleError):
        await service.create_role(db, key="   ", name="Nameless", scope=RoleScope.GLOBAL)


async def test_unknown_role_lookup_raises(db) -> None:
    with pytest.raises(RoleNotFoundError):
        await service.get_role(db, "nonexistent")


async def test_system_roles_cannot_be_deleted(db) -> None:
    """Dropping super_admin would leave nobody able to grant it back."""
    await service.seed_system_roles(db)
    with pytest.raises(RoleConflictError, match="system role"):
        await service.delete_role(db, "super_admin")


async def test_custom_role_can_be_deleted(db) -> None:
    await service.create_role(db, key="auditor", name="Auditor", scope=RoleScope.GLOBAL)
    await service.delete_role(db, "auditor")
    with pytest.raises(RoleNotFoundError):
        await service.get_role(db, "auditor")


async def test_role_still_held_cannot_be_deleted(db) -> None:
    """Deleting would cascade the grants away silently."""
    await service.create_role(db, key="auditor", name="Auditor", scope=RoleScope.GLOBAL)
    user = await _user(db)
    await service.assign_role(db, user_id=user.id, role_key="auditor", granted_by_id=None)

    with pytest.raises(RoleConflictError, match="still held"):
        await service.delete_role(db, "auditor")


# ── granting ───────────────────────────────────────────────────────────


async def test_grants_a_global_role(db) -> None:
    await service.seed_system_roles(db)
    user = await _user(db)
    await service.assign_role(db, user_id=user.id, role_key="manager", granted_by_id=None)

    assert await service.global_role_keys(db, user.id) == {"manager"}


async def test_records_who_granted_it(db) -> None:
    await service.seed_system_roles(db)
    actor = await _user(db, "boss@hamdaz.com")
    target = await _user(db, "staff@hamdaz.com")

    grant = await service.assign_role(
        db, user_id=target.id, role_key="manager", granted_by_id=actor.id
    )
    assert grant.granted_by_id == actor.id


async def test_granting_twice_is_idempotent(db) -> None:
    await service.seed_system_roles(db)
    user = await _user(db)
    first = await service.assign_role(db, user_id=user.id, role_key="ceo", granted_by_id=None)
    second = await service.assign_role(db, user_id=user.id, role_key="ceo", granted_by_id=None)

    assert first.id == second.id
    assert await service.global_role_keys(db, user.id) == {"ceo"}


async def test_a_user_can_hold_several_global_roles(db) -> None:
    await service.seed_system_roles(db)
    user = await _user(db)
    for key in ("ceo", "manager"):
        await service.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)

    assert await service.global_role_keys(db, user.id) == {"ceo", "manager"}


async def test_team_roles_cannot_be_granted_globally(db) -> None:
    """"team_lead" with no team would silently mean "lead of everything"."""
    await service.seed_system_roles(db)
    user = await _user(db)

    with pytest.raises(RoleConflictError, match="team role"):
        await service.assign_role(db, user_id=user.id, role_key="team_lead", granted_by_id=None)


async def test_granting_to_an_unknown_user_raises(db) -> None:
    await service.seed_system_roles(db)
    with pytest.raises(RoleNotFoundError, match="user"):
        await service.assign_role(
            db, user_id=uuid.uuid4(), role_key="manager", granted_by_id=None
        )


async def test_granting_an_unknown_role_raises(db) -> None:
    user = await _user(db)
    with pytest.raises(RoleNotFoundError):
        await service.assign_role(db, user_id=user.id, role_key="wizard", granted_by_id=None)


# ── revoking ───────────────────────────────────────────────────────────


async def test_revokes_a_role(db) -> None:
    await service.seed_system_roles(db)
    user = await _user(db)
    await service.assign_role(db, user_id=user.id, role_key="manager", granted_by_id=None)

    await service.revoke_role(db, user_id=user.id, role_key="manager")
    assert await service.global_role_keys(db, user.id) == set()


async def test_revoking_a_role_not_held_raises(db) -> None:
    await service.seed_system_roles(db)
    user = await _user(db)
    with pytest.raises(RoleNotFoundError):
        await service.revoke_role(db, user_id=user.id, role_key="manager")


async def test_the_last_super_admin_cannot_be_demoted(db) -> None:
    """Otherwise nobody is left who can grant it back — a permanent lockout."""
    await service.seed_system_roles(db)
    only = await _user(db, "boss@hamdaz.com")
    await service.assign_role(db, user_id=only.id, role_key="super_admin", granted_by_id=None)

    with pytest.raises(RoleConflictError, match="last super admin"):
        await service.revoke_role(db, user_id=only.id, role_key="super_admin")


async def test_a_super_admin_can_be_demoted_once_there_are_two(db) -> None:
    await service.seed_system_roles(db)
    first = await _user(db, "one@hamdaz.com")
    second = await _user(db, "two@hamdaz.com")
    for u in (first, second):
        await service.assign_role(db, user_id=u.id, role_key="super_admin", granted_by_id=None)

    await service.revoke_role(db, user_id=second.id, role_key="super_admin")
    assert await service.count_super_admins(db) == 1


async def test_revoking_a_grant_does_not_delete_the_role(db) -> None:
    await service.seed_system_roles(db)
    user = await _user(db)
    await service.assign_role(db, user_id=user.id, role_key="manager", granted_by_id=None)
    await service.revoke_role(db, user_id=user.id, role_key="manager")

    assert await service.get_role(db, "manager") is not None


async def test_deleting_a_user_removes_their_grants_not_the_role(db) -> None:
    await service.seed_system_roles(db)
    user = await _user(db)
    await service.assign_role(db, user_id=user.id, role_key="manager", granted_by_id=None)
    await db.commit()

    await db.delete(user)
    await db.commit()

    assert await service.get_role(db, "manager") is not None
    assert await service.count_super_admins(db) == 0


async def test_a_grant_survives_the_granter_being_deleted(db) -> None:
    """Removing an admin must not quietly strip everyone they ever promoted."""
    await service.seed_system_roles(db)
    granter = await _user(db, "boss@hamdaz.com")
    target = await _user(db, "staff@hamdaz.com")
    await service.assign_role(db, user_id=target.id, role_key="manager", granted_by_id=granter.id)
    await db.commit()

    await db.delete(granter)
    await db.commit()

    assert await service.global_role_keys(db, target.id) == {"manager"}


# ── listing ────────────────────────────────────────────────────────────


async def test_lists_only_users_who_hold_a_role(db) -> None:
    await service.seed_system_roles(db)
    held = await _user(db, "boss@hamdaz.com")
    await _user(db, "nobody@hamdaz.com")
    await service.assign_role(db, user_id=held.id, role_key="ceo", granted_by_id=None)

    assignments = await service.list_assignments(db)
    assert [u.email for u, _ in assignments] == ["boss@hamdaz.com"]


async def test_assignments_are_empty_when_nobody_holds_a_role(db) -> None:
    await service.seed_system_roles(db)
    await _user(db)
    assert await service.list_assignments(db) == []


async def test_list_roles_can_filter_by_scope(db) -> None:
    await service.seed_system_roles(db)
    team = await service.list_roles(db, scope=RoleScope.TEAM)
    assert {r.key for r in team} == {"team_lead", "member", "approver"}


async def test_unique_constraint_backs_the_idempotency(db) -> None:
    """Belt and braces: the database refuses a duplicate grant too."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.models.role import UserRole

    await service.seed_system_roles(db)
    user = await _user(db)
    # select(Role), not Role.__table__.select(): the latter yields the first
    # column, not the mapped object.
    role = await db.scalar(select(Role).where(Role.key == "manager"))
    await service.assign_role(db, user_id=user.id, role_key="manager", granted_by_id=None)
    await db.commit()

    db.add(UserRole(user_id=user.id, role_id=role.id))
    with pytest.raises(IntegrityError):
        await db.commit()
