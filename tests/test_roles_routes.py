"""The roles HTTP surface, with emphasis on who is refused."""

from __future__ import annotations

import uuid

import pytest

from app.auth.deps import SESSION_AUDIENCE
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.core.config import get_settings
from app.core.security import sign
from app.roles import service

SESSION_COOKIE = "hamdaz_session"


async def _make(db, email: str, *role_keys: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in role_keys:
        await service.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)
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


@pytest.fixture
async def roles(db):
    await service.seed_system_roles(db)
    await db.commit()


@pytest.fixture
async def super_admin(db, roles):
    return await _make(db, "boss@hamdaz.com", "super_admin")


@pytest.fixture
async def manager(db, roles):
    return await _make(db, "manager@hamdaz.com", "manager")


@pytest.fixture
async def nobody(db, roles):
    return await _make(db, "nobody@hamdaz.com")


# ── reading ────────────────────────────────────────────────────────────


async def test_listing_roles_requires_a_session(client, roles) -> None:
    assert (await client.get("/api/v1/roles")).status_code == 401


async def test_any_signed_in_user_can_list_roles(client, nobody) -> None:
    response = await _as(client, nobody).get("/api/v1/roles")
    assert response.status_code == 200
    assert {r["key"] for r in response.json()} == {
        "super_admin", "ceo", "manager", "team_lead", "member", "approver",
    }


async def test_roles_can_be_filtered_by_scope(client, nobody) -> None:
    response = await _as(client, nobody).get("/api/v1/roles", params={"scope": "team"})
    assert {r["key"] for r in response.json()} == {"team_lead", "member", "approver"}


async def test_me_reports_no_roles_for_a_plain_user(client, nobody) -> None:
    body = (await _as(client, nobody).get("/api/v1/roles/me")).json()
    assert body["role_keys"] == []
    assert body["is_admin"] is False
    assert body["is_super_admin"] is False


async def test_me_reports_super_admin(client, super_admin) -> None:
    body = (await _as(client, super_admin).get("/api/v1/roles/me")).json()
    assert body["role_keys"] == ["super_admin"]
    assert body["is_admin"] is True
    assert body["is_super_admin"] is True


async def test_me_marks_a_manager_as_admin_but_not_super(client, manager) -> None:
    body = (await _as(client, manager).get("/api/v1/roles/me")).json()
    assert body["is_admin"] is True
    assert body["is_super_admin"] is False


async def test_assignments_lists_holders(client, super_admin) -> None:
    body = (await _as(client, super_admin).get("/api/v1/roles/assignments")).json()
    assert [u["email"] for u in body] == ["boss@hamdaz.com"]
    assert body[0]["role_keys"] == ["super_admin"]


async def test_unknown_user_roles_is_404(client, super_admin) -> None:
    response = await _as(client, super_admin).get(f"/api/v1/roles/users/{uuid.uuid4()}")
    assert response.status_code == 404


# ── creating roles ─────────────────────────────────────────────────────


async def test_a_plain_user_cannot_create_a_role(client, nobody) -> None:
    response = await _as(client, nobody).post(
        "/api/v1/roles", json={"key": "auditor", "name": "Auditor", "scope": "global"}
    )
    assert response.status_code == 403


async def test_an_admin_can_create_a_role(client, manager) -> None:
    response = await _as(client, manager).post(
        "/api/v1/roles", json={"key": "auditor", "name": "Auditor", "scope": "global"}
    )
    assert response.status_code == 201
    assert response.json()["is_system"] is False


@pytest.mark.parametrize("key", ["Auditor", "2fast", "has space", "has-dash", "x"])
async def test_malformed_role_keys_are_rejected(client, manager, key: str) -> None:
    response = await _as(client, manager).post(
        "/api/v1/roles", json={"key": key, "name": "X", "scope": "global"}
    )
    assert response.status_code == 422


async def test_deleting_a_system_role_is_a_conflict(client, super_admin) -> None:
    response = await _as(client, super_admin).delete("/api/v1/roles/manager")
    assert response.status_code == 409


async def test_a_plain_user_cannot_delete_a_role(client, nobody) -> None:
    assert (await _as(client, nobody).delete("/api/v1/roles/manager")).status_code == 403


# ── granting ───────────────────────────────────────────────────────────


async def test_a_plain_user_cannot_grant_anything(client, db, nobody) -> None:
    target = await _make(db, "target@hamdaz.com")
    response = await _as(client, nobody).post(
        f"/api/v1/roles/users/{target.id}", json={"role_key": "manager"}
    )
    assert response.status_code == 403


async def test_a_plain_user_cannot_promote_themselves(client, nobody) -> None:
    """The obvious attack, stated as its own test."""
    response = await _as(client, nobody).post(
        f"/api/v1/roles/users/{nobody.id}", json={"role_key": "manager"}
    )
    assert response.status_code == 403


async def test_an_admin_can_grant_a_global_role(client, db, manager) -> None:
    target = await _make(db, "target@hamdaz.com")
    response = await _as(client, manager).post(
        f"/api/v1/roles/users/{target.id}", json={"role_key": "ceo"}
    )
    assert response.status_code == 201
    assert response.json()["role_keys"] == ["ceo"]


async def test_a_grant_records_the_actor(client, db, manager) -> None:
    target = await _make(db, "target@hamdaz.com")
    body = (
        await _as(client, manager).post(
            f"/api/v1/roles/users/{target.id}", json={"role_key": "ceo"}
        )
    ).json()
    assert body["roles"][0]["granted_by_id"] == str(manager.id)


async def test_a_manager_cannot_grant_super_admin(client, db, manager) -> None:
    """Otherwise 'super admin' means nothing — any manager could mint one."""
    target = await _make(db, "target@hamdaz.com")
    response = await _as(client, manager).post(
        f"/api/v1/roles/users/{target.id}", json={"role_key": "super_admin"}
    )
    assert response.status_code == 403
    assert "super admin" in response.json()["detail"].lower()


async def test_a_manager_cannot_promote_themselves_to_super_admin(client, manager) -> None:
    response = await _as(client, manager).post(
        f"/api/v1/roles/users/{manager.id}", json={"role_key": "super_admin"}
    )
    assert response.status_code == 403


async def test_a_super_admin_can_grant_super_admin(client, db, super_admin) -> None:
    target = await _make(db, "target@hamdaz.com")
    response = await _as(client, super_admin).post(
        f"/api/v1/roles/users/{target.id}", json={"role_key": "super_admin"}
    )
    assert response.status_code == 201


async def test_granting_a_team_role_globally_is_a_conflict(client, db, super_admin) -> None:
    target = await _make(db, "target@hamdaz.com")
    response = await _as(client, super_admin).post(
        f"/api/v1/roles/users/{target.id}", json={"role_key": "team_lead"}
    )
    assert response.status_code == 409


async def test_granting_an_unknown_role_is_404(client, db, super_admin) -> None:
    target = await _make(db, "target@hamdaz.com")
    response = await _as(client, super_admin).post(
        f"/api/v1/roles/users/{target.id}", json={"role_key": "wizard"}
    )
    assert response.status_code == 404


async def test_granting_to_an_unknown_user_is_404(client, super_admin) -> None:
    response = await _as(client, super_admin).post(
        f"/api/v1/roles/users/{uuid.uuid4()}", json={"role_key": "manager"}
    )
    assert response.status_code == 404


# ── revoking ───────────────────────────────────────────────────────────


async def test_an_admin_can_revoke_a_global_role(client, db, manager) -> None:
    target = await _make(db, "target@hamdaz.com", "ceo")
    response = await _as(client, manager).delete(f"/api/v1/roles/users/{target.id}/ceo")
    assert response.status_code == 204

    body = (await _as(client, manager).get(f"/api/v1/roles/users/{target.id}")).json()
    assert body["role_keys"] == []


async def test_a_manager_cannot_revoke_super_admin(client, super_admin, db) -> None:
    mgr = await _make(db, "manager@hamdaz.com", "manager")
    response = await _as(client, mgr).delete(f"/api/v1/roles/users/{super_admin.id}/super_admin")
    assert response.status_code == 403


async def test_the_last_super_admin_cannot_be_revoked(client, super_admin) -> None:
    response = await _as(client, super_admin).delete(
        f"/api/v1/roles/users/{super_admin.id}/super_admin"
    )
    assert response.status_code == 409
    assert "last super admin" in response.json()["detail"].lower()


async def test_a_super_admin_can_step_down_once_there_are_two(client, db, super_admin) -> None:
    second = await _make(db, "second@hamdaz.com", "super_admin")
    response = await _as(client, super_admin).delete(
        f"/api/v1/roles/users/{second.id}/super_admin"
    )
    assert response.status_code == 204


async def test_revoking_a_role_not_held_is_404(client, db, manager) -> None:
    target = await _make(db, "target@hamdaz.com")
    response = await _as(client, manager).delete(f"/api/v1/roles/users/{target.id}/ceo")
    assert response.status_code == 404


async def test_a_plain_user_cannot_revoke(client, db, nobody) -> None:
    target = await _make(db, "target@hamdaz.com", "ceo")
    assert (
        await _as(client, nobody).delete(f"/api/v1/roles/users/{target.id}/ceo")
    ).status_code == 403


# ── the guard sees changes immediately ─────────────────────────────────


async def test_losing_a_role_takes_effect_on_the_next_request(client, db, super_admin) -> None:
    """No cached permission set: a demotion must bite at once."""
    target = await _make(db, "target@hamdaz.com", "manager")
    other = await _make(db, "other@hamdaz.com")

    assert (
        await _as(client, target).post(
            f"/api/v1/roles/users/{other.id}", json={"role_key": "ceo"}
        )
    ).status_code == 201

    await _as(client, super_admin).delete(f"/api/v1/roles/users/{target.id}/manager")

    assert (
        await _as(client, target).post(
            f"/api/v1/roles/users/{other.id}", json={"role_key": "manager"}
        )
    ).status_code == 403


# ── granting to someone who has never signed in ────────────────────────


async def test_can_grant_by_entra_object_id(client, db, super_admin, graph) -> None:
    """The reported bug: an admin picks someone out of the directory and grants.

    The directory hands back an Entra object id, which looks exactly like a
    local users.id, so this used to 404.
    """
    import uuid as _uuid

    from app.directory.graph import OrgUser

    oid = str(_uuid.uuid4())
    graph.users = [
        OrgUser(
            object_id=oid,
            display_name="Never Loggedin",
            email="never@hamdaz.com",
            user_principal_name="never@hamdaz.com",
            job_title=None,
            department=None,
            office_location=None,
            mobile_phone=None,
            account_enabled=True,
            user_type="Member",
        )
    ]

    response = await _as(client, super_admin).post(
        f"/api/v1/roles/users/{oid}", json={"role_key": "manager"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "never@hamdaz.com"
    assert body["role_keys"] == ["manager"]
    # The response carries the *local* id, not the one that was posted.
    assert body["user_id"] != oid


async def test_an_id_in_neither_place_still_404s(client, super_admin, graph) -> None:
    import uuid as _uuid

    graph.users = []
    response = await _as(client, super_admin).post(
        f"/api/v1/roles/users/{_uuid.uuid4()}", json={"role_key": "manager"}
    )
    assert response.status_code == 404
    assert "directory" in response.json()["detail"]


async def test_reading_roles_never_provisions(client, db, super_admin, graph) -> None:
    """A GET must not create a user as a side effect."""
    import uuid as _uuid

    from sqlalchemy import func, select

    from app.models.user import User

    before = await db.scalar(select(func.count()).select_from(User))
    response = await _as(client, super_admin).get(f"/api/v1/roles/users/{_uuid.uuid4()}")

    assert response.status_code == 404
    assert await db.scalar(select(func.count()).select_from(User)) == before
