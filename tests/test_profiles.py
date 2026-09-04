"""The all-in-one user profile, and resetting a user.

Two things are worth guarding hardest here: that one broken section cannot sink
the whole profile, and that a reset removes *every* registered section's data —
because the failure mode of a purge is leftover rows nobody knows about.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.directory.graph import OrgUser
from app.profiles import registry
from app.roles import service as roles
from app.teams import service as teams

SESSION_COOKIE = "hamdaz_session"
BASE = "/api/v1/users"


async def _make(db, email: str, *role_keys: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in role_keys:
        await roles.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)
    await db.commit()
    return user


def _as(client, user):
    client.cookies.set(
        SESSION_COOKIE,
        sign(
            {"sub": str(user.id)},
            secret=get_settings().session_secret,
            ttl_minutes=60,
            audience=SESSION_AUDIENCE,
        ),
    )
    return client


def _org(oid: str, name: str = "Person") -> OrgUser:
    return OrgUser(
        object_id=oid, display_name=name, email=f"{name.lower()}@hamdaz.com",
        user_principal_name=f"{name.lower()}@hamdaz.com", job_title="Engineer",
        department="Delivery", office_location=None, mobile_phone=None,
        account_enabled=True, user_type="Member",
    )


@pytest.fixture
async def seeded(db):
    await roles.seed_system_roles(db)
    await db.commit()


@pytest.fixture
async def admin(db, seeded):
    return await _make(db, "boss@hamdaz.com", "super_admin")


@pytest.fixture
async def subject(db, seeded):
    """Someone with data in every module."""
    user = await _make(db, "subject@hamdaz.com", "manager")
    team = await teams.create_team(db, name="Delivery")
    await teams.set_member_roles(
        db, team=team, user=user, role_keys=["team_lead", "approver"]
    )
    await db.commit()
    return user


# ── the registry ───────────────────────────────────────────────────────


def test_every_module_is_registered() -> None:
    assert set(registry.section_keys()) >= {
        "identity", "directory", "roles", "teams", "activity"
    }


def test_directory_is_not_resettable() -> None:
    """That record belongs to Entra. Resetting an ERP user must not touch it."""
    directory = next(s for s in registry.all_sections() if s.key == "directory")
    assert directory.purge is None
    assert directory.remote is True


def test_resolve_rejects_an_unknown_section() -> None:
    with pytest.raises(KeyError, match="payroll"):
        registry.resolve(["payroll"])


def test_resolve_can_drop_remote_sections() -> None:
    keys = [s.key for s in registry.resolve(None, include_remote=False)]
    assert "directory" not in keys
    assert "roles" in keys


# ── reading a profile ──────────────────────────────────────────────────


async def test_profile_gathers_every_section(client, admin, subject, graph) -> None:
    graph.users = [_org(subject.entra_object_id, "Subject")]
    body = (await _as(client, admin).get(f"{BASE}/{subject.id}")).json()

    assert set(body["sections"]) == {"identity", "directory", "roles", "teams", "activity"}
    assert body["errors"] == {}
    assert body["sections"]["roles"]["keys"] == ["manager"]
    assert body["sections"]["teams"]["count"] == 1
    assert body["sections"]["teams"]["memberships"][0]["role_keys"] == ["approver", "team_lead"]
    assert body["sections"]["teams"]["memberships"][0]["is_lead"] is True


async def test_profile_reports_per_section_timings(client, admin, subject) -> None:
    """The numbers that tell you which section got slow."""
    meta = (await _as(client, admin).get(f"{BASE}/{subject.id}")).json()["meta"]
    assert set(meta["section_ms"]) == set(meta["requested"])
    # Fanned out, so the total cannot exceed the sum of the parts.
    assert meta["elapsed_ms"] <= sum(meta["section_ms"].values()) + 50


@pytest.mark.parametrize("lookup", ["id", "email", "entra"])
async def test_a_user_can_be_found_three_ways(client, admin, subject, lookup: str) -> None:
    ref = {
        "id": str(subject.id),
        "email": subject.email,
        "entra": subject.entra_object_id,
    }[lookup]
    res = await _as(client, admin).get(f"{BASE}/{ref}")
    assert res.status_code == 200
    assert res.json()["user_id"] == str(subject.id)


async def test_include_narrows_the_response(client, admin, subject) -> None:
    body = (await _as(client, admin).get(f"{BASE}/{subject.id}?include=roles,teams")).json()
    assert set(body["sections"]) == {"roles", "teams"}


async def test_local_only_skips_the_directory_call(client, admin, subject, graph) -> None:
    body = (await _as(client, admin).get(f"{BASE}/{subject.id}?local_only=true")).json()
    assert "directory" not in body["sections"]
    assert graph.calls == []  # Graph was never asked


async def test_an_unknown_section_is_a_400_that_lists_the_valid_ones(
    client, admin, subject
) -> None:
    res = await _as(client, admin).get(f"{BASE}/{subject.id}?include=payroll")
    assert res.status_code == 400
    assert "identity" in res.json()["detail"]


async def test_a_broken_section_does_not_sink_the_profile(client, admin, subject, graph) -> None:
    """Graph being down must still leave every local section usable."""
    from app.directory.graph import GraphError

    graph.error = GraphError("Graph returned 503")
    body = (await _as(client, admin).get(f"{BASE}/{subject.id}")).json()

    assert body["sections"]["directory"] is None
    assert body["sections"]["roles"]["keys"] == ["manager"]
    assert body["sections"]["teams"]["count"] == 1


async def test_someone_not_in_the_directory_still_has_a_profile(
    client, admin, subject, graph
) -> None:
    graph.users = []
    body = (await _as(client, admin).get(f"{BASE}/{subject.id}")).json()
    assert body["sections"]["directory"] is None
    assert body["sections"]["identity"]["email"] == "subject@hamdaz.com"


async def test_profile_requires_a_session(client, subject) -> None:
    assert (await client.get(f"{BASE}/{subject.id}")).status_code == 401


async def test_an_unknown_user_is_404(client, admin) -> None:
    assert (await _as(client, admin).get(f"{BASE}/{uuid.uuid4()}")).status_code == 404


async def test_sections_endpoint_describes_the_registry(client, admin) -> None:
    body = (await _as(client, admin).get(f"{BASE}/sections")).json()
    by_key = {s["key"]: s for s in body}
    assert by_key["roles"]["resettable"] is True
    assert by_key["directory"]["resettable"] is False
    assert by_key["identity"]["remote"] is False


# ── resetting ──────────────────────────────────────────────────────────


async def test_reset_strips_roles_and_teams_but_keeps_the_account(client, admin, subject) -> None:
    res = await _as(client, admin).post(f"{BASE}/{subject.id}/reset")
    assert res.status_code == 200

    body = res.json()
    assert body["account_deleted"] is False
    assert body["removed"]["roles"] == 1
    assert body["removed"]["teams"] == 2  # team_lead + approver

    after = (await _as(client, admin).get(f"{BASE}/{subject.id}?local_only=true")).json()
    assert after["sections"]["roles"]["keys"] == []
    assert after["sections"]["teams"]["count"] == 0
    assert after["sections"]["identity"]["email"] == "subject@hamdaz.com"


async def test_reset_reports_what_was_removed(client, admin, subject) -> None:
    """An admin doing this deserves to see what they destroyed."""
    body = (await _as(client, admin).post(f"{BASE}/{subject.id}/reset")).json()
    assert body["previous"]["roles"]["keys"] == ["manager"]
    assert body["previous"]["teams"]["count"] == 1


async def test_reset_covers_every_resettable_section(client, admin, subject) -> None:
    """Guards the failure mode: a new module whose data survives a reset."""
    body = (await _as(client, admin).post(f"{BASE}/{subject.id}/reset")).json()
    expected = {s.key for s in registry.purgeable()}
    assert set(body["removed"]) == expected


async def test_reset_is_idempotent(client, admin, subject) -> None:
    await _as(client, admin).post(f"{BASE}/{subject.id}/reset")
    body = (await _as(client, admin).post(f"{BASE}/{subject.id}/reset")).json()
    assert body["removed"] == {"roles": 0, "teams": 0}


async def test_a_plain_user_cannot_reset_anyone(client, db, seeded, subject) -> None:
    nobody = await _make(db, "nobody@hamdaz.com")
    assert (
        await _as(client, nobody).post(f"{BASE}/{subject.id}/reset")
    ).status_code == 403


async def test_you_cannot_reset_yourself(client, admin) -> None:
    """A footgun with no legitimate use: ask another admin."""
    res = await _as(client, admin).post(f"{BASE}/{admin.id}/reset")
    assert res.status_code == 409
    assert "your own account" in res.json()["detail"]


async def test_the_last_super_admin_cannot_be_reset(client, db, seeded, admin) -> None:
    other = await _make(db, "other@hamdaz.com", "super_admin")
    # Demote the fixture admin so `other` is the only one left.
    await roles.revoke_role(db, user_id=admin.id, role_key="super_admin")
    await roles.assign_role(db, user_id=admin.id, role_key="manager", granted_by_id=None)
    await db.commit()

    res = await _as(client, admin).post(f"{BASE}/{other.id}/reset")
    assert res.status_code == 409
    assert "last super admin" in res.json()["detail"]


# ── deleting ───────────────────────────────────────────────────────────


async def test_delete_removes_the_account_entirely(client, admin, subject) -> None:
    res = await _as(client, admin).delete(f"{BASE}/{subject.id}")
    assert res.status_code == 200
    assert res.json()["account_deleted"] is True

    assert (await _as(client, admin).get(f"{BASE}/{subject.id}")).status_code == 404


async def test_delete_reports_everything_it_removed(client, admin, subject) -> None:
    body = (await _as(client, admin).delete(f"{BASE}/{subject.id}")).json()
    assert body["removed"]["roles"] == 1
    assert body["removed"]["teams"] == 2
    assert body["removed"]["identity"] == 1


async def test_delete_leaves_the_team_intact(client, admin, subject, db) -> None:
    """Removing a person must not remove the team they were leading."""
    await _as(client, admin).delete(f"{BASE}/{subject.id}")
    team = await teams.get_team(db, "delivery")
    assert await teams.list_members(db, team.id) == []


async def test_a_super_admin_must_be_demoted_before_deletion(client, db, seeded, admin) -> None:
    other = await _make(db, "other@hamdaz.com", "super_admin")
    res = await _as(client, admin).delete(f"{BASE}/{other.id}")
    assert res.status_code == 409
    assert "Revoke super admin" in res.json()["detail"]


async def test_you_cannot_delete_yourself(client, admin) -> None:
    assert (await _as(client, admin).delete(f"{BASE}/{admin.id}")).status_code == 409


async def test_a_plain_user_cannot_delete_anyone(client, db, seeded, subject) -> None:
    nobody = await _make(db, "nobody@hamdaz.com")
    assert (await _as(client, nobody).delete(f"{BASE}/{subject.id}")).status_code == 403


async def test_signing_in_after_deletion_creates_a_fresh_empty_account(
    client, admin, subject, db
) -> None:
    """Deleting removes them from the ERP, not from the company."""
    oid = subject.entra_object_id
    await _as(client, admin).delete(f"{BASE}/{subject.id}")

    returning = await upsert_user(
        db, EntraIdentity(object_id=oid, email="subject@hamdaz.com", display_name="Subject")
    )
    await db.commit()

    assert returning.id != subject.id
    assert await roles.global_role_keys(db, returning.id) == set()
